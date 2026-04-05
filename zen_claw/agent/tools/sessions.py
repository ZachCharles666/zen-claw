"""Background session tools backed by sec-execd sidecar."""

from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlparse

import httpx

from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult
from zen_claw.agent.tools.sidecar_approval import build_hmac_approval_headers, hmac_body_json
from zen_claw.security_context import gateway_instance_id, stable_json_hash


class _SidecarSessionsBase(Tool):
    def __init__(
        self,
        sidecar_exec_url: str,
        sidecar_approval_mode: str = "token",
        sidecar_approval_token: str = "",
        sidecar_healthcheck: bool = False,
    ):
        self.sidecar_exec_url = sidecar_exec_url
        self.sidecar_approval_mode = sidecar_approval_mode
        self.sidecar_approval_token = sidecar_approval_token
        self.sidecar_healthcheck = sidecar_healthcheck

    def _healthz_url(self) -> str:
        parsed = urlparse(self.sidecar_exec_url)
        path = parsed.path
        if path.endswith("/v1/exec"):
            path = path[: -len("/v1/exec")] + "/healthz"
        else:
            path = "/healthz"
        return parsed._replace(path=path, params="", query="", fragment="").geturl()

    def _sessions_url(self, suffix: str = "") -> str:
        parsed = urlparse(self.sidecar_exec_url)
        path = parsed.path
        if path.endswith("/v1/exec"):
            base_path = path[: -len("/v1/exec")]
        else:
            base_path = ""
        target = f"{base_path}/v1/sessions{suffix}"
        return parsed._replace(path=target, params="", query="", fragment="").geturl()

    def _hmac_headers(
        self,
        *,
        trace_id: str,
        method: str,
        path: str,
        body_bytes: bytes,
        policy_snapshot: dict[str, Any],
        gateway_instance: str,
    ) -> dict[str, str] | None:
        if self.sidecar_approval_mode != "hmac":
            return None
        if not trace_id or not self.sidecar_approval_token:
            return None
        return build_hmac_approval_headers(
            secret=self.sidecar_approval_token,
            trace_id=trace_id,
            method=method,
            path=path,
            body_bytes=body_bytes,
            policy_snapshot=policy_snapshot,
            gateway_instance=gateway_instance,
        )

    def _prepare_request(
        self,
        *,
        trace_id: str,
        method: str,
        path: str,
        payload: dict[str, Any],
        security_context: dict[str, Any] | None,
    ) -> tuple[dict[str, str], bytes, dict[str, Any]] | ToolResult:
        sec = dict(security_context or {})
        policy_snapshot = dict(sec.get("policy_snapshot") or {})
        policy_snapshot_hash = str(
            sec.get("policy_snapshot_hash")
            or policy_snapshot.get("policy_snapshot_hash")
            or stable_json_hash(policy_snapshot)
        )
        missing_sec = [
            key
            for key in (
                "tenant_id",
                "workspace_id",
                "agent_profile",
                "channel",
                "sender_id",
                "chat_id",
                "role",
                "trust_level",
                "origin_surface",
            )
            if not str(sec.get(key) or "").strip()
        ]
        if missing_sec:
            return ToolResult.failure(
                ToolErrorKind.PERMISSION,
                f"sessions sidecar requires complete security context: missing {', '.join(missing_sec)}",
                code="sessions_security_context_missing",
            )
        if not trace_id:
            return ToolResult.failure(
                ToolErrorKind.PERMISSION,
                "trace_id is required for sessions sidecar requests",
                code="sessions_trace_required",
            )
        if not policy_snapshot_hash:
            return ToolResult.failure(
                ToolErrorKind.PERMISSION,
                "sessions sidecar requires policy snapshot hash",
                code="sessions_policy_snapshot_missing",
            )
        gateway_signature = str(sec.get("gateway_signature") or "").strip()
        if not gateway_signature:
            return ToolResult.failure(
                ToolErrorKind.PERMISSION,
                "sessions sidecar requires gateway signature",
                code="sessions_gateway_signature_missing",
            )
        request_payload = {
            **dict(payload),
            "security_context": sec,
            "policy_snapshot": policy_snapshot,
            "gateway_signature": gateway_signature,
            "gateway_meta": {
                "gateway_instance": str(sec.get("gateway_instance") or gateway_instance_id()),
                "policy_snapshot_hash": policy_snapshot_hash,
            },
        }
        request_payload["gateway_meta"]["request_hash"] = stable_json_hash(request_payload)
        body_bytes = hmac_body_json(request_payload)
        headers = {
            "Content-Type": "application/json",
            "X-Trace-Id": trace_id,
            "X-Gateway-Request-Hash": hashlib.sha256(body_bytes).hexdigest(),
            "X-Policy-Snapshot-Hash": policy_snapshot_hash,
            "X-Gateway-Instance": str(request_payload["gateway_meta"]["gateway_instance"]),
        }
        if self.sidecar_approval_mode == "hmac":
            if not self.sidecar_approval_token:
                return ToolResult.failure(
                    ToolErrorKind.PERMISSION,
                    "sidecar_approval_token must be set as HMAC secret for hmac mode",
                    code="sessions_secret_missing",
                )
            headers.update(
                self._hmac_headers(
                    trace_id=trace_id,
                    method=method,
                    path=path,
                    body_bytes=body_bytes,
                    policy_snapshot=policy_snapshot,
                    gateway_instance=str(request_payload["gateway_meta"]["gateway_instance"]),
                )
                or {}
            )
        elif self.sidecar_approval_token:
            headers["X-Approval-Token"] = self.sidecar_approval_token
        return headers, body_bytes, request_payload

    async def _check_health(self, client: httpx.AsyncClient) -> ToolResult | None:
        if not self.sidecar_healthcheck:
            return None
        health = await client.get(self._healthz_url())
        if health.status_code < 400:
            return None
        return ToolResult.failure(
            ToolErrorKind.RETRYABLE,
            f"Sidecar health check failed with HTTP {health.status_code}",
            code="sessions_sidecar_unhealthy",
        )

    def _error_kind_from_status(self, status: int) -> ToolErrorKind:
        if status in (401, 403):
            return ToolErrorKind.PERMISSION
        if status in (400, 404):
            return ToolErrorKind.PARAMETER
        if status in (502, 503, 504):
            return ToolErrorKind.RETRYABLE
        return ToolErrorKind.RUNTIME


class SessionsSpawnTool(_SidecarSessionsBase):
    @property
    def name(self) -> str:
        return "sessions_spawn"

    @property
    def description(self) -> str:
        return "Start a background shell session via sec-execd sidecar."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run in background"},
                "working_dir": {"type": "string", "description": "Optional working directory"},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600},
                "pty": {
                    "type": "boolean",
                    "description": "Request pseudo-terminal mode for interactive-ish commands",
                },
            },
            "required": ["command"],
        }

    async def execute(
        self,
        command: str,
        working_dir: str | None = None,
        timeout_seconds: int | None = None,
        pty: bool | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        trace_id = str(kwargs.get("trace_id") or "")
        payload: dict[str, Any] = {"command": command}
        if working_dir:
            payload["working_dir"] = working_dir
        if isinstance(timeout_seconds, int) and timeout_seconds > 0:
            payload["timeout_seconds"] = timeout_seconds
        if isinstance(pty, bool):
            payload["pty"] = pty
        prepared = self._prepare_request(
            trace_id=trace_id,
            method="POST",
            path="/v1/sessions/start",
            payload=payload,
            security_context=kwargs.get("security_context"),
        )
        if isinstance(prepared, ToolResult):
            return prepared
        headers, body_bytes, request_payload = prepared
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                unhealthy = await self._check_health(client)
                if unhealthy:
                    return unhealthy
                resp = await client.request(
                    "POST",
                    self._sessions_url("/start"),
                    headers=headers,
                    content=body_bytes if self.sidecar_approval_mode == "hmac" else None,
                    json=None if self.sidecar_approval_mode == "hmac" else request_payload,
                )
        except httpx.TimeoutException as e:
            return ToolResult.failure(
                ToolErrorKind.RETRYABLE, f"Sidecar request timed out: {e}", code="sessions_timeout"
            )
        except httpx.RequestError as e:
            return ToolResult.failure(
                ToolErrorKind.RETRYABLE, f"Sidecar request failed: {e}", code="sessions_unreachable"
            )

        try:
            data = resp.json()
        except Exception:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                "Sidecar returned invalid JSON",
                code="sessions_invalid_response",
            )

        if resp.status_code >= 400:
            return ToolResult.failure(
                self._error_kind_from_status(resp.status_code),
                str(data.get("error_message") or f"HTTP {resp.status_code}"),
                code=str(data.get("error_code") or "sessions_spawn_failed"),
                http_status=resp.status_code,
            )

        session_id = str(data.get("session_id") or "")
        status = str(data.get("status") or "running")
        return ToolResult.success(
            f"Started session {session_id} ({status})",
            session_id=session_id,
            status=status,
            sidecar=True,
            sidecar_target=self.sidecar_exec_url,
        )


class SessionsListTool(_SidecarSessionsBase):
    @property
    def name(self) -> str:
        return "sessions_list"

    @property
    def description(self) -> str:
        return "List background sessions from sec-execd sidecar."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> ToolResult:
        trace_id = str(kwargs.get("trace_id") or "")
        prepared = self._prepare_request(
            trace_id=trace_id,
            method="GET",
            path="/v1/sessions",
            payload={},
            security_context=kwargs.get("security_context"),
        )
        if isinstance(prepared, ToolResult):
            return prepared
        headers, body_bytes, request_payload = prepared
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                unhealthy = await self._check_health(client)
                if unhealthy:
                    return unhealthy
                resp = await client.request(
                    "GET",
                    self._sessions_url(),
                    headers=headers,
                    content=body_bytes if self.sidecar_approval_mode == "hmac" else None,
                    json=None if self.sidecar_approval_mode == "hmac" else request_payload,
                )
        except httpx.TimeoutException as e:
            return ToolResult.failure(
                ToolErrorKind.RETRYABLE, f"Sidecar request timed out: {e}", code="sessions_timeout"
            )
        except httpx.RequestError as e:
            return ToolResult.failure(
                ToolErrorKind.RETRYABLE, f"Sidecar request failed: {e}", code="sessions_unreachable"
            )

        try:
            data = resp.json()
        except Exception:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                "Sidecar returned invalid JSON",
                code="sessions_invalid_response",
            )

        if resp.status_code >= 400:
            return ToolResult.failure(
                self._error_kind_from_status(resp.status_code),
                str(data.get("error_message") or f"HTTP {resp.status_code}"),
                code=str(data.get("error_code") or "sessions_list_failed"),
                http_status=resp.status_code,
            )

        sessions = data.get("sessions")
        if not isinstance(sessions, list):
            return ToolResult.failure(
                ToolErrorKind.RUNTIME, "Invalid sessions payload", code="sessions_invalid_payload"
            )
        if not sessions:
            return ToolResult.success("No sessions.", sessions=[])

        lines = []
        for s in sessions:
            sid = str(s.get("session_id") or "")
            status = str(s.get("status") or "")
            cmd = str(s.get("command") or "")
            lines.append(f"- {sid} [{status}] {cmd}")
        return ToolResult.success(
            "Sessions:\n" + "\n".join(lines),
            sessions=sessions,
            sidecar=True,
            sidecar_target=self.sidecar_exec_url,
        )


class SessionsKillTool(_SidecarSessionsBase):
    @property
    def name(self) -> str:
        return "sessions_kill"

    @property
    def description(self) -> str:
        return "Kill a running background session via sec-execd sidecar."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session ID to kill"},
            },
            "required": ["session_id"],
        }

    async def execute(self, session_id: str, **kwargs: Any) -> ToolResult:
        trace_id = str(kwargs.get("trace_id") or "")
        prepared = self._prepare_request(
            trace_id=trace_id,
            method="POST",
            path=f"/v1/sessions/{session_id}/kill",
            payload={"session_id": session_id},
            security_context=kwargs.get("security_context"),
        )
        if isinstance(prepared, ToolResult):
            return prepared
        headers, body_bytes, request_payload = prepared
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                unhealthy = await self._check_health(client)
                if unhealthy:
                    return unhealthy
                resp = await client.request(
                    "POST",
                    self._sessions_url(f"/{session_id}/kill"),
                    headers=headers,
                    content=body_bytes if self.sidecar_approval_mode == "hmac" else None,
                    json=None if self.sidecar_approval_mode == "hmac" else request_payload,
                )
        except httpx.TimeoutException as e:
            return ToolResult.failure(
                ToolErrorKind.RETRYABLE, f"Sidecar request timed out: {e}", code="sessions_timeout"
            )
        except httpx.RequestError as e:
            return ToolResult.failure(
                ToolErrorKind.RETRYABLE, f"Sidecar request failed: {e}", code="sessions_unreachable"
            )

        try:
            data = resp.json()
        except Exception:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                "Sidecar returned invalid JSON",
                code="sessions_invalid_response",
            )

        if resp.status_code >= 400:
            return ToolResult.failure(
                self._error_kind_from_status(resp.status_code),
                str(data.get("error_message") or f"HTTP {resp.status_code}"),
                code=str(data.get("error_code") or "sessions_kill_failed"),
                http_status=resp.status_code,
            )

        killed = bool(data.get("killed"))
        if killed:
            return ToolResult.success(
                f"Killed session {session_id}",
                session_id=session_id,
                sidecar=True,
                sidecar_target=self.sidecar_exec_url,
            )
        return ToolResult.success(
            f"Session {session_id} is not running",
            session_id=session_id,
            sidecar=True,
            sidecar_target=self.sidecar_exec_url,
        )


class SessionsWriteTool(_SidecarSessionsBase):
    @property
    def name(self) -> str:
        return "sessions_write"

    @property
    def description(self) -> str:
        return "Write input to a running background session stdin via sec-execd sidecar."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session ID to write into"},
                "input": {"type": "string", "description": "Input text to send to session stdin"},
            },
            "required": ["session_id", "input"],
        }

    async def execute(self, session_id: str, input: str, **kwargs: Any) -> ToolResult:
        trace_id = str(kwargs.get("trace_id") or "")
        payload: dict[str, Any] = {"input": input}
        prepared = self._prepare_request(
            trace_id=trace_id,
            method="POST",
            path=f"/v1/sessions/{session_id}/write",
            payload=payload,
            security_context=kwargs.get("security_context"),
        )
        if isinstance(prepared, ToolResult):
            return prepared
        headers, body_bytes, request_payload = prepared
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                unhealthy = await self._check_health(client)
                if unhealthy:
                    return unhealthy
                resp = await client.request(
                    "POST",
                    self._sessions_url(f"/{session_id}/write"),
                    headers=headers,
                    content=body_bytes if self.sidecar_approval_mode == "hmac" else None,
                    json=None if self.sidecar_approval_mode == "hmac" else request_payload,
                )
        except httpx.TimeoutException as e:
            return ToolResult.failure(
                ToolErrorKind.RETRYABLE, f"Sidecar request timed out: {e}", code="sessions_timeout"
            )
        except httpx.RequestError as e:
            return ToolResult.failure(
                ToolErrorKind.RETRYABLE, f"Sidecar request failed: {e}", code="sessions_unreachable"
            )

        try:
            data = resp.json()
        except Exception:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                "Sidecar returned invalid JSON",
                code="sessions_invalid_response",
            )

        if resp.status_code >= 400:
            return ToolResult.failure(
                self._error_kind_from_status(resp.status_code),
                str(data.get("error_message") or f"HTTP {resp.status_code}"),
                code=str(data.get("error_code") or "sessions_write_failed"),
                http_status=resp.status_code,
            )

        written_bytes = int(data.get("written_bytes") or 0)
        status = str(data.get("status") or "running")
        return ToolResult.success(
            f"Wrote {written_bytes} bytes to session {session_id} (status={status})",
            session_id=session_id,
            status=status,
            written_bytes=written_bytes,
            sidecar=True,
            sidecar_target=self.sidecar_exec_url,
        )


class SessionsReadTool(_SidecarSessionsBase):
    @property
    def name(self) -> str:
        return "sessions_read"

    @property
    def description(self) -> str:
        return "Read incremental output from a background session via sec-execd sidecar."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session ID to read"},
                "cursor": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Read cursor offset (default: 0)",
                },
                "max_bytes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 8192,
                    "description": "Maximum bytes to read",
                },
            },
            "required": ["session_id"],
        }

    async def execute(
        self,
        session_id: str,
        cursor: int | None = None,
        max_bytes: int | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        trace_id = str(kwargs.get("trace_id") or "")
        payload: dict[str, Any] = {"session_id": session_id}
        if isinstance(cursor, int):
            payload["cursor"] = cursor
        if isinstance(max_bytes, int):
            payload["max_bytes"] = max_bytes
        prepared = self._prepare_request(
            trace_id=trace_id,
            method="GET",
            path=f"/v1/sessions/{session_id}/read",
            payload=payload,
            security_context=kwargs.get("security_context"),
        )
        if isinstance(prepared, ToolResult):
            return prepared
        headers, body_bytes, request_payload = prepared

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                unhealthy = await self._check_health(client)
                if unhealthy:
                    return unhealthy
                resp = await client.request(
                    "GET",
                    self._sessions_url(f"/{session_id}/read"),
                    headers=headers,
                    content=body_bytes if self.sidecar_approval_mode == "hmac" else None,
                    json=None if self.sidecar_approval_mode == "hmac" else request_payload,
                )
        except httpx.TimeoutException as e:
            return ToolResult.failure(
                ToolErrorKind.RETRYABLE, f"Sidecar request timed out: {e}", code="sessions_timeout"
            )
        except httpx.RequestError as e:
            return ToolResult.failure(
                ToolErrorKind.RETRYABLE, f"Sidecar request failed: {e}", code="sessions_unreachable"
            )

        try:
            data = resp.json()
        except Exception:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                "Sidecar returned invalid JSON",
                code="sessions_invalid_response",
            )

        if resp.status_code >= 400:
            return ToolResult.failure(
                self._error_kind_from_status(resp.status_code),
                str(data.get("error_message") or f"HTTP {resp.status_code}"),
                code=str(data.get("error_code") or "sessions_read_failed"),
                http_status=resp.status_code,
            )

        chunk = str(data.get("chunk") or "")
        next_cursor = int(data.get("next_cursor") or 0)
        status = str(data.get("status") or "running")
        truncated = bool(data.get("truncated"))
        if not chunk:
            return ToolResult.success(
                f"No new output (status={status}, next_cursor={next_cursor})",
                session_id=session_id,
                status=status,
                chunk="",
                next_cursor=next_cursor,
                truncated=truncated,
                sidecar=True,
                sidecar_target=self.sidecar_exec_url,
            )
        return ToolResult.success(
            chunk,
            session_id=session_id,
            status=status,
            chunk=chunk,
            next_cursor=next_cursor,
            truncated=truncated,
            sidecar=True,
            sidecar_target=self.sidecar_exec_url,
        )


class SessionsSignalTool(_SidecarSessionsBase):
    @property
    def name(self) -> str:
        return "sessions_signal"

    @property
    def description(self) -> str:
        return "Send control signal (interrupt/terminate/kill) to a running background session."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session ID to signal"},
                "signal": {
                    "type": "string",
                    "enum": ["interrupt", "terminate", "kill"],
                    "description": "Signal action to deliver",
                },
            },
            "required": ["session_id"],
        }

    async def execute(
        self, session_id: str, signal: str | None = None, **kwargs: Any
    ) -> ToolResult:
        trace_id = str(kwargs.get("trace_id") or "")
        payload: dict[str, Any] = {"signal": str(signal or "interrupt")}
        prepared = self._prepare_request(
            trace_id=trace_id,
            method="POST",
            path=f"/v1/sessions/{session_id}/signal",
            payload=payload,
            security_context=kwargs.get("security_context"),
        )
        if isinstance(prepared, ToolResult):
            return prepared
        headers, body_bytes, request_payload = prepared
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                unhealthy = await self._check_health(client)
                if unhealthy:
                    return unhealthy
                resp = await client.request(
                    "POST",
                    self._sessions_url(f"/{session_id}/signal"),
                    headers=headers,
                    content=body_bytes if self.sidecar_approval_mode == "hmac" else None,
                    json=None if self.sidecar_approval_mode == "hmac" else request_payload,
                )
        except httpx.TimeoutException as e:
            return ToolResult.failure(
                ToolErrorKind.RETRYABLE, f"Sidecar request timed out: {e}", code="sessions_timeout"
            )
        except httpx.RequestError as e:
            return ToolResult.failure(
                ToolErrorKind.RETRYABLE, f"Sidecar request failed: {e}", code="sessions_unreachable"
            )

        try:
            data = resp.json()
        except Exception:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                "Sidecar returned invalid JSON",
                code="sessions_invalid_response",
            )

        if resp.status_code >= 400:
            return ToolResult.failure(
                self._error_kind_from_status(resp.status_code),
                str(data.get("error_message") or f"HTTP {resp.status_code}"),
                code=str(data.get("error_code") or "sessions_signal_failed"),
                http_status=resp.status_code,
            )

        delivered = bool(data.get("delivered"))
        status = str(data.get("status") or "running")
        sig = str(data.get("signal") or payload["signal"])
        if delivered:
            return ToolResult.success(
                f"Delivered {sig} to session {session_id}",
                session_id=session_id,
                status=status,
                signal=sig,
                delivered=delivered,
                sidecar=True,
                sidecar_target=self.sidecar_exec_url,
            )
        return ToolResult.success(
            f"Signal {sig} accepted for session {session_id} (status={status})",
            session_id=session_id,
            status=status,
            signal=sig,
            delivered=delivered,
            sidecar=True,
            sidecar_target=self.sidecar_exec_url,
        )


class SessionsResizeTool(_SidecarSessionsBase):
    @property
    def name(self) -> str:
        return "sessions_resize"

    @property
    def description(self) -> str:
        return "Resize PTY dimensions for a running background session."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session ID to resize"},
                "rows": {"type": "integer", "minimum": 1, "maximum": 1000},
                "cols": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            "required": ["session_id", "rows", "cols"],
        }

    async def execute(self, session_id: str, rows: int, cols: int, **kwargs: Any) -> ToolResult:
        trace_id = str(kwargs.get("trace_id") or "")
        payload: dict[str, Any] = {"rows": rows, "cols": cols}
        prepared = self._prepare_request(
            trace_id=trace_id,
            method="POST",
            path=f"/v1/sessions/{session_id}/resize",
            payload=payload,
            security_context=kwargs.get("security_context"),
        )
        if isinstance(prepared, ToolResult):
            return prepared
        headers, body_bytes, request_payload = prepared
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                unhealthy = await self._check_health(client)
                if unhealthy:
                    return unhealthy
                resp = await client.request(
                    "POST",
                    self._sessions_url(f"/{session_id}/resize"),
                    headers=headers,
                    content=body_bytes if self.sidecar_approval_mode == "hmac" else None,
                    json=None if self.sidecar_approval_mode == "hmac" else request_payload,
                )
        except httpx.TimeoutException as e:
            return ToolResult.failure(
                ToolErrorKind.RETRYABLE, f"Sidecar request timed out: {e}", code="sessions_timeout"
            )
        except httpx.RequestError as e:
            return ToolResult.failure(
                ToolErrorKind.RETRYABLE, f"Sidecar request failed: {e}", code="sessions_unreachable"
            )

        try:
            data = resp.json()
        except Exception:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                "Sidecar returned invalid JSON",
                code="sessions_invalid_response",
            )

        if resp.status_code >= 400:
            return ToolResult.failure(
                self._error_kind_from_status(resp.status_code),
                str(data.get("error_message") or f"HTTP {resp.status_code}"),
                code=str(data.get("error_code") or "sessions_resize_failed"),
                http_status=resp.status_code,
            )

        applied = bool(data.get("applied"))
        status = str(data.get("status") or "running")
        return ToolResult.success(
            f"Resized session {session_id} to {rows}x{cols} (applied={str(applied).lower()}, status={status})",
            session_id=session_id,
            status=status,
            rows=rows,
            cols=cols,
            applied=applied,
            sidecar=True,
            sidecar_target=self.sidecar_exec_url,
        )

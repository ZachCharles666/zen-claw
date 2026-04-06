"""Browser automation tools via sidecar transport."""

import hashlib
import json
from typing import Any
from urllib.parse import urlparse

import httpx

from zen_claw.agent.tools._url_guard import is_domain_blocked
from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult
from zen_claw.agent.tools.sidecar_approval import build_hmac_approval_headers, hmac_body_json
from zen_claw.security_context import (
    canonical_gateway_envelope_hash,
    canonical_policy_snapshot_hash,
    gateway_instance_id,
    sign_gateway_envelope,
)


def _healthz_from_sidecar_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or ""
    if path.endswith("/v1/browser"):
        path = path[: -len("/v1/browser")] + "/healthz"
    else:
        path = "/healthz"
    return parsed._replace(path=path, params="", query="", fragment="").geturl()


class _BrowserSidecarBase(Tool):
    mode: str = "off"
    sidecar_url: str = "http://127.0.0.1:4500/v1/browser"
    sidecar_approval_mode: str = "hmac"
    sidecar_healthcheck: bool = False
    sidecar_fallback_to_off: bool = False
    allowed_domains: list[str]
    blocked_domains: list[str]
    max_steps: int
    timeout_sec: int
    action_name: str = ""

    def __init__(
        self,
        *,
        mode: str = "off",
        sidecar_url: str = "http://127.0.0.1:4500/v1/browser",
        sidecar_approval_mode: str = "hmac",
        sidecar_approval_token: str = "",
        sidecar_healthcheck: bool = False,
        sidecar_fallback_to_off: bool = False,
        allowed_domains: list[str] | None = None,
        blocked_domains: list[str] | None = None,
        max_steps: int = 20,
        timeout_sec: int = 30,
    ):
        self.mode = mode
        self.sidecar_url = sidecar_url
        self.sidecar_approval_mode = sidecar_approval_mode
        self.sidecar_approval_token = sidecar_approval_token
        self.sidecar_healthcheck = sidecar_healthcheck
        self.sidecar_fallback_to_off = sidecar_fallback_to_off
        self.allowed_domains = list(allowed_domains or [])
        self.blocked_domains = list(blocked_domains or [])
        self.max_steps = max(1, int(max_steps))
        self.timeout_sec = max(1, int(timeout_sec))

    async def _execute_action(
        self,
        payload: dict[str, Any],
        trace_id: str,
        *,
        override_max_steps: int | None = None,
    ) -> ToolResult:
        if self.mode != "sidecar":
            return ToolResult.failure(
                ToolErrorKind.PERMISSION,
                "browser automation is disabled by config (tools.network.browser.mode=off)",
                code="browser_disabled",
            )

        security_context = dict(payload.pop("__security_context__", {}) or {})
        policy_snapshot = dict(security_context.get("policy_snapshot") or {})
        policy_snapshot_hash = str(
            security_context.get("policy_snapshot_hash")
            or policy_snapshot.get("policy_snapshot_hash")
            or canonical_policy_snapshot_hash(policy_snapshot)
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
            if not str(security_context.get(key) or "").strip()
        ]
        if missing_sec:
            return ToolResult.failure(
                ToolErrorKind.PERMISSION,
                f"browser sidecar requires complete security context: missing {', '.join(missing_sec)}",
                code="browser_sidecar_security_context_missing",
            )
        if not policy_snapshot_hash:
            return ToolResult.failure(
                ToolErrorKind.PERMISSION,
                "browser sidecar requires policy snapshot hash",
                code="browser_sidecar_policy_snapshot_missing",
            )
        upstream_gateway_signature = str(security_context.get("gateway_signature") or "").strip()
        if not upstream_gateway_signature:
            return ToolResult.failure(
                ToolErrorKind.PERMISSION,
                "browser sidecar requires gateway signature",
                code="browser_sidecar_gateway_signature_missing",
            )
        effective_max_steps = (
            max(1, int(override_max_steps)) if override_max_steps is not None else self.max_steps
        )
        req = {
            "action": self.action_name,
            "payload": payload,
            "security_context": security_context,
            "policy_snapshot": policy_snapshot,
            "policy": {
                "allowed_domains": self.allowed_domains,
                "blocked_domains": self.blocked_domains,
                "max_steps": effective_max_steps,
            },
            "gateway_meta": {
                "gateway_instance": str(security_context.get("gateway_instance") or gateway_instance_id()),
                "policy_snapshot_hash": policy_snapshot_hash,
            },
        }
        req["gateway_meta"]["request_hash"] = canonical_gateway_envelope_hash(req)
        req["gateway_signature"] = sign_gateway_envelope(req)
        headers = {"Content-Type": "application/json"}
        body_bytes = hmac_body_json(req)
        if self.sidecar_approval_mode == "hmac":
            if not trace_id:
                return ToolResult.failure(
                    ToolErrorKind.PERMISSION,
                    "browser sidecar hmac mode requires trace_id",
                    code="browser_trace_required",
                )
            if not self.sidecar_approval_token:
                return ToolResult.failure(
                    ToolErrorKind.PERMISSION,
                    "browser sidecar hmac mode requires approval token",
                    code="browser_secret_missing",
                )
            headers.update(
                build_hmac_approval_headers(
                    secret=self.sidecar_approval_token,
                    trace_id=trace_id,
                    method="POST",
                    path="/v1/browser",
                    body_bytes=body_bytes,
                    policy_snapshot=policy_snapshot,
                    gateway_instance=str(req["gateway_meta"]["gateway_instance"]),
                )
            )
        else:
            if trace_id:
                headers["X-Trace-Id"] = trace_id
            if self.sidecar_approval_token:
                headers["X-Approval-Token"] = self.sidecar_approval_token
        headers["X-Gateway-Request-Hash"] = hashlib.sha256(body_bytes).hexdigest()
        headers["X-Policy-Snapshot-Hash"] = policy_snapshot_hash
        headers["X-Gateway-Instance"] = str(req["gateway_meta"]["gateway_instance"])

        try:
            async with httpx.AsyncClient(
                timeout=float(self.timeout_sec), proxy=None, trust_env=False
            ) as client:
                if self.sidecar_healthcheck:
                    health = await client.get(_healthz_from_sidecar_url(self.sidecar_url))
                    if health.status_code >= 400:
                        return self._health_error(health.status_code)
                response = await client.post(
                    self.sidecar_url,
                    headers=headers,
                    content=body_bytes if self.sidecar_approval_mode == "hmac" else None,
                    json=None if self.sidecar_approval_mode == "hmac" else req,
                )
        except httpx.TimeoutException as e:
            return self._transport_error(str(e), code="browser_sidecar_timeout")
        except httpx.RequestError as e:
            return self._transport_error(str(e), code="browser_sidecar_unreachable")

        try:
            data = response.json()
        except Exception as exc:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                f"Browser sidecar returned invalid JSON ({exc}) HTTP {response.status_code}: {response.text}",
                code="browser_sidecar_invalid_response",
            )

        if response.status_code >= 400:
            code = str(data.get("error_code") or "browser_sidecar_error")
            msg = str(data.get("error") or f"HTTP {response.status_code}")
            kind = (
                ToolErrorKind.PERMISSION if response.status_code == 403 else ToolErrorKind.RUNTIME
            )
            return ToolResult.failure(kind, msg, code=code, http_status=response.status_code)

        if not bool(data.get("ok", True)):
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                str(data.get("error") or "browser action failed"),
                code=str(data.get("error_code") or "browser_action_failed"),
            )

        # Response-layer domain guard: validate the URL the sidecar actually landed on
        # (may differ from the requested URL due to redirects).
        final_url = str(data.get("finalUrl") or data.get("url") or "")
        if final_url and self.blocked_domains and is_domain_blocked(final_url, self.blocked_domains):
            return ToolResult.failure(
                ToolErrorKind.PERMISSION,
                f"Browser response URL '{final_url}' is blocked by domain policy",
                code="browser_blocked_domain",
            )

        return ToolResult.success(json.dumps(data, ensure_ascii=False))

    def _health_error(self, status_code: int) -> ToolResult:
        if self.sidecar_fallback_to_off:
            return ToolResult.failure(
                ToolErrorKind.PERMISSION,
                "browser automation unavailable (sidecar health failed; fallback=off)",
                code="browser_unavailable_fallback_off",
            )
        return ToolResult.failure(
            ToolErrorKind.RUNTIME,
            f"Browser sidecar health check failed with HTTP {status_code}",
            code="browser_sidecar_unhealthy",
        )

    def _transport_error(self, message: str, *, code: str) -> ToolResult:
        if self.sidecar_fallback_to_off:
            return ToolResult.failure(
                ToolErrorKind.PERMISSION,
                "browser automation unavailable (sidecar unreachable; fallback=off)",
                code="browser_unavailable_fallback_off",
            )
        return ToolResult.failure(ToolErrorKind.RUNTIME, message, code=code)


class BrowserOpenTool(_BrowserSidecarBase):
    name = "browser_open"
    description = "Open a web page in the browser sidecar session."
    action_name = "open"
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Target URL"},
            "sessionId": {"type": "string", "description": "Optional session id"},
            "maxSteps": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "description": "Override max steps for this call.",
            },
        },
        "required": ["url"],
    }

    async def execute(  # noqa: N803
        self,
        url: str,
        sessionId: str | None = None,  # noqa: N803
        maxSteps: int | None = None,  # noqa: N803
        **kwargs: Any,
    ) -> ToolResult:
        trace_id = str(kwargs.get("trace_id") or "")
        payload = {"url": url, "__security_context__": dict(kwargs.get("security_context") or {})}
        if sessionId:
            payload["session_id"] = sessionId
        return await self._execute_action(payload, trace_id, override_max_steps=maxSteps)


class BrowserExtractTool(_BrowserSidecarBase):
    name = "browser_extract"
    description = "Extract text/content from current or specified browser page."
    action_name = "extract"
    parameters = {
        "type": "object",
        "properties": {
            "sessionId": {"type": "string", "description": "Optional session id"},
            "selector": {"type": "string", "description": "Optional CSS selector"},
            "maxChars": {"type": "integer", "minimum": 100, "maximum": 200000},
            "maxSteps": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "description": "Override max steps for this call.",
            },
        },
        "required": [],
    }

    async def execute(  # noqa: N803
        self,
        sessionId: str | None = None,  # noqa: N803
        selector: str | None = None,
        maxChars: int | None = None,  # noqa: N803
        maxSteps: int | None = None,  # noqa: N803
        **kwargs: Any,
    ) -> ToolResult:
        trace_id = str(kwargs.get("trace_id") or "")
        payload: dict[str, Any] = {"__security_context__": dict(kwargs.get("security_context") or {})}
        if sessionId:
            payload["session_id"] = sessionId
        if selector:
            payload["selector"] = selector
        if maxChars is not None:
            payload["max_chars"] = int(maxChars)
        return await self._execute_action(payload, trace_id, override_max_steps=maxSteps)


class BrowserScreenshotTool(_BrowserSidecarBase):
    name = "browser_screenshot"
    description = "Take a screenshot from browser sidecar session."
    action_name = "screenshot"
    parameters = {
        "type": "object",
        "properties": {
            "sessionId": {"type": "string", "description": "Optional session id"},
            "fullPage": {"type": "boolean", "description": "Capture full page"},
            "maxSteps": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "description": "Override max steps for this call.",
            },
        },
        "required": [],
    }

    async def execute(  # noqa: N803
        self,
        sessionId: str | None = None,  # noqa: N803
        fullPage: bool = False,  # noqa: N803
        maxSteps: int | None = None,  # noqa: N803
        **kwargs: Any,
    ) -> ToolResult:
        trace_id = str(kwargs.get("trace_id") or "")
        payload: dict[str, Any] = {
            "full_page": bool(fullPage),
            "__security_context__": dict(kwargs.get("security_context") or {}),
        }
        if sessionId:
            payload["session_id"] = sessionId
        return await self._execute_action(payload, trace_id, override_max_steps=maxSteps)


class BrowserClickTool(_BrowserSidecarBase):
    name = "browser_click"
    description = "Click an element in browser sidecar session."
    action_name = "click"
    parameters = {
        "type": "object",
        "properties": {
            "sessionId": {"type": "string", "description": "Session id"},
            "selector": {"type": "string", "description": "CSS selector to click"},
            "maxSteps": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "description": "Override max steps for this call.",
            },
        },
        "required": ["sessionId", "selector"],
    }

    async def execute(  # noqa: N803
        self,
        sessionId: str,  # noqa: N803
        selector: str,
        maxSteps: int | None = None,  # noqa: N803
        **kwargs: Any,
    ) -> ToolResult:
        trace_id = str(kwargs.get("trace_id") or "")
        payload: dict[str, Any] = {
            "session_id": sessionId,
            "selector": selector,
            "__security_context__": dict(kwargs.get("security_context") or {}),
        }
        return await self._execute_action(payload, trace_id, override_max_steps=maxSteps)


class BrowserTypeTool(_BrowserSidecarBase):
    name = "browser_type"
    description = "Type text into an element in browser sidecar session."
    action_name = "type"
    parameters = {
        "type": "object",
        "properties": {
            "sessionId": {"type": "string", "description": "Session id"},
            "selector": {"type": "string", "description": "CSS selector"},
            "text": {"type": "string", "description": "Text to input"},
            "clear": {"type": "boolean", "description": "Clear input before typing"},
            "maxSteps": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "description": "Override max steps for this call.",
            },
        },
        "required": ["sessionId", "selector", "text"],
    }

    async def execute(  # noqa: N803
        self,
        sessionId: str,  # noqa: N803
        selector: str,
        text: str,
        clear: bool = True,
        maxSteps: int | None = None,  # noqa: N803
        **kwargs: Any,
    ) -> ToolResult:
        trace_id = str(kwargs.get("trace_id") or "")
        payload: dict[str, Any] = {
            "session_id": sessionId,
            "selector": selector,
            "text": text,
            "clear": bool(clear),
            "__security_context__": dict(kwargs.get("security_context") or {}),
        }
        return await self._execute_action(payload, trace_id, override_max_steps=maxSteps)


class BrowserSaveSessionTool(_BrowserSidecarBase):
    name = "browser_save_session"
    description = "Save browser session state (cookies/localStorage) to disk."
    action_name = "save_session"
    parameters = {
        "type": "object",
        "properties": {
            "sessionId": {"type": "string", "description": "Session id to save"},
        },
        "required": ["sessionId"],
    }

    async def execute(self, sessionId: str, **kwargs: Any) -> ToolResult:  # noqa: N803
        trace_id = str(kwargs.get("trace_id") or "")
        payload: dict[str, Any] = {
            "session_id": sessionId,
            "__security_context__": dict(kwargs.get("security_context") or {}),
        }
        return await self._execute_action(payload, trace_id)


class BrowserLoadSessionTool(_BrowserSidecarBase):
    name = "browser_load_session"
    description = "Load browser session state from disk and return a new session id."
    action_name = "load_session"
    parameters = {
        "type": "object",
        "properties": {
            "sessionId": {"type": "string", "description": "Session id used when saving state"},
            "stateFile": {"type": "string", "description": "Explicit state file path"},
        },
        "required": [],
    }

    async def execute(  # noqa: N803
        self,
        sessionId: str | None = None,  # noqa: N803
        stateFile: str | None = None,  # noqa: N803
        **kwargs: Any,
    ) -> ToolResult:
        trace_id = str(kwargs.get("trace_id") or "")
        payload: dict[str, Any] = {"__security_context__": dict(kwargs.get("security_context") or {})}
        has_state_reference = False
        if stateFile:
            payload["state_file"] = stateFile
            has_state_reference = True
        if sessionId:
            payload["session_id"] = sessionId
            has_state_reference = True
        if not has_state_reference:
            return ToolResult.failure(
                ToolErrorKind.PARAMETER,
                "Either sessionId or stateFile must be provided",
                code="browser_load_session_missing_args",
            )
        return await self._execute_action(payload, trace_id)

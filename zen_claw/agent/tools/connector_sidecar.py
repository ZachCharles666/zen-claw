"""Gateway-sidecar client for connector and outbound write operations."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlparse

import httpx

from zen_claw.agent.tools.result import ToolErrorKind, ToolResult
from zen_claw.agent.tools.sidecar_approval import build_hmac_approval_headers, hmac_body_json
from zen_claw.security_context import (
    gateway_instance_id,
    resource_scope_for_security_context,
    stable_json_hash,
)


class ConnectorSidecarClient:
    """Send connector write operations through the gateway-sidecar path."""

    def __init__(
        self,
        *,
        proxy_url: str,
        approval_mode: str = "token",
        approval_token: str = "",
        timeout_sec: float = 20.0,
    ) -> None:
        self.proxy_url = str(proxy_url or "").strip()
        self.approval_mode = str(approval_mode or "token").strip().lower()
        self.approval_token = str(approval_token or "")
        self.timeout_sec = float(timeout_sec)

    def build_request_payload(
        self,
        *,
        target_url: str,
        method: str,
        headers: dict[str, str] | None,
        body: str,
        connector_name: str,
        action: str,
        target_resource: str,
        security_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        sec = dict(security_context or {})
        policy_snapshot = dict(sec.get("policy_snapshot") or {})
        policy_snapshot_hash = str(
            sec.get("policy_snapshot_hash")
            or policy_snapshot.get("policy_snapshot_hash")
            or stable_json_hash(policy_snapshot)
        )
        request_payload = {
            "url": target_url,
            "method": str(method or "POST").strip().upper() or "POST",
            "headers": dict(headers or {}),
            "body": body,
            "connector_meta": {
                "connector_name": str(connector_name or "").strip().lower(),
                "action": str(action or "").strip().lower(),
                "target_resource": str(target_resource or "").strip().lower(),
                "identity": {
                    "tenant_id": str(sec.get("tenant_id") or "").strip().lower(),
                    "workspace_id": str(sec.get("workspace_id") or "").strip(),
                    "agent_profile": str(sec.get("agent_profile") or "").strip().lower(),
                    "channel": str(sec.get("channel") or "").strip().lower(),
                    "sender_id": str(sec.get("sender_id") or "").strip(),
                    "chat_id": str(sec.get("chat_id") or "").strip(),
                    "role": str(sec.get("role") or "").strip().lower(),
                    "trust_level": str(sec.get("trust_level") or "").strip().lower(),
                    "origin_surface": str(sec.get("origin_surface") or "").strip().lower(),
                    "trace_id": str(sec.get("trace_id") or "").strip(),
                },
                "policy_snapshot": policy_snapshot,
                "policy_snapshot_hash": policy_snapshot_hash,
                "resource_scope": {
                    **resource_scope_for_security_context(sec),
                    "connector_name": str(connector_name or "").strip().lower(),
                    "action": str(action or "").strip().lower(),
                    "target_resource": str(target_resource or "").strip().lower(),
                },
            },
            "gateway_meta": {
                "gateway_instance": str(sec.get("gateway_instance") or gateway_instance_id()),
                "policy_snapshot_hash": policy_snapshot_hash,
            },
        }
        request_payload["gateway_meta"]["request_hash"] = stable_json_hash(request_payload)
        return request_payload

    def build_proxy_headers(
        self,
        *,
        request_payload: dict[str, Any],
        trace_id: str,
    ) -> tuple[dict[str, str], bytes] | ToolResult:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        body_bytes = hmac_body_json(request_payload)
        proxy_path = str(urlparse(self.proxy_url).path or "/").strip() or "/"
        gateway_meta = dict(request_payload.get("gateway_meta") or {})
        policy_snapshot_hash = str(gateway_meta.get("policy_snapshot_hash") or "")
        request_hash = hashlib.sha256(body_bytes).hexdigest()
        if self.approval_mode == "hmac":
            if not trace_id:
                return ToolResult.failure(
                    ToolErrorKind.PERMISSION,
                    "connector sidecar hmac mode requires trace_id",
                    code="connector_sidecar_trace_required",
                )
            if not self.approval_token:
                return ToolResult.failure(
                    ToolErrorKind.PERMISSION,
                    "connector sidecar hmac mode requires approval secret",
                    code="connector_sidecar_secret_missing",
                )
            headers.update(
                build_hmac_approval_headers(
                    secret=self.approval_token,
                    trace_id=trace_id,
                    method="POST",
                    path=proxy_path,
                    body_bytes=body_bytes,
                    policy_snapshot=request_payload.get("connector_meta", {}).get("policy_snapshot"),
                    gateway_instance=str(gateway_meta.get("gateway_instance") or ""),
                )
            )
        else:
            if trace_id:
                headers["X-Trace-Id"] = trace_id
            if self.approval_token:
                headers["X-Approval-Token"] = self.approval_token
        if request_hash:
            headers["X-Gateway-Request-Hash"] = request_hash
        if policy_snapshot_hash:
            headers["X-Policy-Snapshot-Hash"] = policy_snapshot_hash
        if gateway_meta.get("gateway_instance"):
            headers["X-Gateway-Instance"] = str(gateway_meta.get("gateway_instance"))
        return headers, body_bytes

    def _parse_response(
        self,
        *,
        status_code: int,
        data: dict[str, Any],
    ) -> ToolResult:
        if status_code >= 400 or not bool(data.get("ok", True)):
            msg = str(data.get("error") or f"HTTP {status_code}")
            code = str(data.get("error_code") or "connector_sidecar_failed")
            kind = ToolErrorKind.PERMISSION if status_code in {401, 403} else ToolErrorKind.RUNTIME
            return ToolResult.failure(
                kind,
                msg,
                code=code,
                http_status=status_code,
                sidecar_target=self.proxy_url,
            )

        body = data.get("body") or ""
        try:
            parsed = json.loads(body) if isinstance(body, str) else body
            return ToolResult.success(
                json.dumps(parsed, ensure_ascii=False),
                http_status=data.get("status"),
                sidecar_target=self.proxy_url,
            )
        except Exception:
            return ToolResult.success(
                str(body),
                http_status=data.get("status"),
                sidecar_target=self.proxy_url,
            )

    async def execute_request(
        self,
        *,
        request_payload: dict[str, Any],
        trace_id: str,
    ) -> ToolResult:
        header_result = self.build_proxy_headers(request_payload=request_payload, trace_id=trace_id)
        if isinstance(header_result, ToolResult):
            return header_result
        connector_meta = dict(request_payload.get("connector_meta") or {})
        identity = dict(connector_meta.get("identity") or {})
        missing_identity = [
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
            if not str(identity.get(key) or "").strip()
        ]
        if missing_identity:
            return ToolResult.failure(
                ToolErrorKind.PERMISSION,
                f"connector sidecar requires complete security context: missing {', '.join(missing_identity)}",
                code="connector_sidecar_security_context_missing",
                sidecar_target=self.proxy_url,
            )
        if not str(connector_meta.get("policy_snapshot_hash") or "").strip():
            return ToolResult.failure(
                ToolErrorKind.PERMISSION,
                "connector sidecar requires policy snapshot hash",
                code="connector_sidecar_policy_snapshot_missing",
                sidecar_target=self.proxy_url,
            )
        headers, body_bytes = header_result

        content = body_bytes if self.approval_mode == "hmac" else None
        json_payload = None if self.approval_mode == "hmac" else request_payload
        try:
            async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
                response = await client.post(
                    self.proxy_url,
                    headers=headers,
                    content=content,
                    json=json_payload,
                )
        except httpx.TimeoutException as exc:
            return ToolResult.failure(
                ToolErrorKind.RETRYABLE,
                str(exc),
                code="connector_sidecar_timeout",
                sidecar_target=self.proxy_url,
            )
        except httpx.RequestError as exc:
            return ToolResult.failure(
                ToolErrorKind.RETRYABLE,
                str(exc),
                code="connector_sidecar_unavailable",
                sidecar_target=self.proxy_url,
            )

        try:
            data = response.json()
        except Exception:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                "connector sidecar returned invalid JSON",
                code="connector_sidecar_invalid_response",
                sidecar_target=self.proxy_url,
            )
        return self._parse_response(status_code=response.status_code, data=data)

    async def execute_write(
        self,
        *,
        target_url: str,
        auth_header: str,
        payload: dict[str, Any],
        connector_name: str,
        action: str,
        target_resource: str,
        trace_id: str,
        security_context: dict[str, Any] | None = None,
    ) -> ToolResult:
        request_payload = self.build_request_payload(
            target_url=target_url,
            method="POST",
            headers={"Authorization": auth_header, "Content-Type": "application/json"},
            body=json.dumps(payload, ensure_ascii=False),
            connector_name=connector_name,
            action=action,
            target_resource=target_resource,
            security_context=security_context,
        )
        return await self.execute_request(request_payload=request_payload, trace_id=trace_id)

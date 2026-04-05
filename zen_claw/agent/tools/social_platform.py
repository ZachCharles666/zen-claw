"""Social platform REST connector tools."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.connector_sidecar import ConnectorSidecarClient
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult
from zen_claw.security_context import build_security_context

_DEFAULT_PROXY_URL = "http://127.0.0.1:4499/v1/fetch"


def _validate_base_url(base_url: str) -> tuple[bool, str]:
    try:
        p = urlparse(str(base_url or "").strip())
    except Exception as exc:
        return False, str(exc)
    if p.scheme not in {"http", "https"}:
        return False, f"Only http/https allowed, got '{p.scheme or 'none'}'"
    if not p.netloc:
        return False, "Missing domain in base_url"
    return True, ""


def _build_social_security_context(
    *,
    trace_id: str,
    security_context: dict[str, Any] | None,
    tenant_id: str | None,
    workspace_id: str | None,
    agent_profile: str | None,
) -> dict[str, Any]:
    sec = dict(security_context or {})
    if tenant_id:
        sec["tenant_id"] = tenant_id
    if workspace_id:
        sec["workspace_id"] = workspace_id
    if agent_profile:
        sec["agent_profile"] = agent_profile
    return build_security_context(
        trace_id=trace_id or str(sec.get("trace_id") or "").strip(),
        channel=str(sec.get("channel") or "cli"),
        sender_id=str(sec.get("sender_id") or "social-agent"),
        chat_id=str(sec.get("chat_id") or "social-direct"),
        tenant_id=str(sec.get("tenant_id") or "default"),
        workspace_id=str(sec.get("workspace_id") or "social"),
        agent_profile=str(sec.get("agent_profile") or "default"),
        role=str(sec.get("role") or "operator"),
        trust_level=str(sec.get("trust_level") or "trusted_local"),
        origin_surface=str(sec.get("origin_surface") or sec.get("channel") or "cli"),
        channel_role=str(sec.get("channel_role") or "operator"),
        workspace_path=str(sec.get("workspace_path") or ""),
        gateway_instance=str(sec.get("gateway_instance") or ""),
        dev_profile=bool(sec.get("dev_profile")),
        trusted_local_only=bool(sec.get("trusted_local_only")),
        policy_snapshot=dict(sec.get("policy_snapshot") or {}),
    )


class SocialPlatformPostTool(Tool):
    name = "social_platform_post"
    description = "POST to social platform REST API endpoint (via net-proxy)."
    parameters = {
        "type": "object",
        "properties": {
            "base_url": {"type": "string"},
            "endpoint": {"type": "string"},
            "payload": {"type": "object"},
            "auth_header": {"type": "string"},
            "connector_name": {"type": "string"},
            "action": {"type": "string"},
            "target_resource": {"type": "string"},
            "tenant_id": {"type": "string"},
            "workspace_id": {"type": "string"},
            "agent_profile": {"type": "string"},
        },
        "required": [
            "base_url",
            "endpoint",
            "payload",
            "auth_header",
            "connector_name",
            "action",
            "target_resource",
        ],
    }

    def __init__(
        self,
        proxy_url: str = _DEFAULT_PROXY_URL,
        timeout_sec: float = 20.0,
        approval_mode: str = "token",
        approval_token: str = "",
    ):
        self._client = ConnectorSidecarClient(
            proxy_url=proxy_url,
            approval_mode=approval_mode,
            approval_token=approval_token,
            timeout_sec=timeout_sec,
        )

    async def execute(
        self,
        base_url: str,
        endpoint: str,
        payload: dict,
        auth_header: str,
        connector_name: str,
        action: str,
        target_resource: str,
        **kwargs: Any,
    ) -> ToolResult:
        valid, err = _validate_base_url(base_url)
        if not valid:
            return ToolResult.failure(ToolErrorKind.PARAMETER, err, code="invalid_base_url")
        ep = str(endpoint or "").strip()
        if not ep.startswith("/"):
            ep = "/" + ep
        target_url = str(base_url).rstrip("/") + ep
        trace_id = str(kwargs.get("trace_id") or "")
        security_context = _build_social_security_context(
            trace_id=trace_id,
            security_context=kwargs.get("security_context"),
            tenant_id=kwargs.get("tenant_id"),
            workspace_id=kwargs.get("workspace_id"),
            agent_profile=kwargs.get("agent_profile"),
        )
        return await self._client.execute_write(
            target_url=target_url,
            auth_header=auth_header,
            payload=payload,
            connector_name=connector_name,
            action=action,
            target_resource=target_resource,
            trace_id=trace_id,
            security_context=security_context,
        )


class SocialPlatformLikeTool(Tool):
    """Like/upvote a post on a social platform (via net-proxy)."""

    name = "social_platform_like"
    description = "Upvote / like a post on a social platform REST API (via net-proxy)."
    parameters = {
        "type": "object",
        "properties": {
            "base_url": {"type": "string", "description": "Base URL of the social platform API."},
            "post_id": {"type": "string", "description": "ID of the post to upvote."},
            "auth_header": {
                "type": "string",
                "description": "Authorization header value (e.g. 'Bearer <token>').",
            },
            "connector_name": {"type": "string"},
            "action": {"type": "string"},
            "target_resource": {"type": "string"},
            "tenant_id": {"type": "string"},
            "workspace_id": {"type": "string"},
            "agent_profile": {"type": "string"},
        },
        "required": [
            "base_url",
            "post_id",
            "auth_header",
            "connector_name",
            "action",
            "target_resource",
        ],
    }

    def __init__(
        self,
        proxy_url: str = _DEFAULT_PROXY_URL,
        timeout_sec: float = 15.0,
        approval_mode: str = "token",
        approval_token: str = "",
    ):
        self._client = ConnectorSidecarClient(
            proxy_url=proxy_url,
            approval_mode=approval_mode,
            approval_token=approval_token,
            timeout_sec=timeout_sec,
        )

    async def execute(
        self,
        base_url: str,
        post_id: str,
        auth_header: str,
        connector_name: str,
        action: str,
        target_resource: str,
        **kwargs: Any,
    ) -> ToolResult:
        valid, err = _validate_base_url(base_url)
        if not valid:
            return ToolResult.failure(ToolErrorKind.PARAMETER, err, code="invalid_base_url")

        pid = str(post_id or "").strip()
        if not pid:
            return ToolResult.failure(
                ToolErrorKind.PARAMETER, "post_id is required", code="missing_post_id"
            )

        target_url = str(base_url).rstrip("/") + f"/api/posts/{pid}/upvote"
        trace_id = str(kwargs.get("trace_id") or "")
        security_context = _build_social_security_context(
            trace_id=trace_id,
            security_context=kwargs.get("security_context"),
            tenant_id=kwargs.get("tenant_id"),
            workspace_id=kwargs.get("workspace_id"),
            agent_profile=kwargs.get("agent_profile"),
        )
        return await self._client.execute_write(
            target_url=target_url,
            auth_header=auth_header,
            payload={},
            connector_name=connector_name,
            action=action,
            target_resource=target_resource,
            trace_id=trace_id,
            security_context=security_context,
        )


class SocialPlatformGetTool(Tool):
    name = "social_platform_get"
    description = "GET from social platform REST API endpoint (via net-proxy)."
    parameters = {
        "type": "object",
        "properties": {
            "base_url": {"type": "string"},
            "endpoint": {"type": "string"},
            "query_params": {"type": "object"},
            "auth_header": {"type": "string"},
        },
        "required": ["base_url", "endpoint", "auth_header"],
    }

    def __init__(self, proxy_url: str = _DEFAULT_PROXY_URL, timeout_sec: float = 15.0):
        self.proxy_url = proxy_url
        self.timeout_sec = float(timeout_sec)

    async def execute(
        self,
        base_url: str,
        endpoint: str,
        auth_header: str,
        query_params: dict | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        valid, err = _validate_base_url(base_url)
        if not valid:
            return ToolResult.failure(ToolErrorKind.PARAMETER, err, code="invalid_base_url")
        ep = str(endpoint or "").strip()
        if not ep.startswith("/"):
            ep = "/" + ep
        target_url = str(base_url).rstrip("/") + ep
        if query_params:
            target_url = target_url + "?" + urlencode({k: str(v) for k, v in query_params.items()})
        trace_id = str(kwargs.get("trace_id") or "")
        req = {
            "url": target_url,
            "method": "GET",
            "headers": {"Authorization": auth_header},
        }
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if trace_id:
            headers["X-Trace-Id"] = trace_id
        try:
            async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
                resp = await client.post(self.proxy_url, headers=headers, json=req)
        except httpx.TimeoutException as exc:
            return ToolResult.failure(ToolErrorKind.RETRYABLE, str(exc), code="social_get_timeout")
        except httpx.RequestError as exc:
            return ToolResult.failure(
                ToolErrorKind.RETRYABLE, str(exc), code="social_get_proxy_unreachable"
            )
        try:
            data = resp.json()
        except Exception:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                "Proxy returned non-JSON response",
                code="social_get_invalid_response",
            )
        if resp.status_code >= 400 or not bool(data.get("ok", True)):
            msg = str(data.get("error") or f"HTTP {resp.status_code}")
            code = str(data.get("error_code") or "social_get_failed")
            kind = (
                ToolErrorKind.PERMISSION
                if resp.status_code in {401, 403}
                else ToolErrorKind.RUNTIME
            )
            return ToolResult.failure(kind, msg, code=code)
        body = data.get("body") or ""
        try:
            parsed = json.loads(body) if isinstance(body, str) else body
            return ToolResult.success(
                json.dumps(parsed, ensure_ascii=False), http_status=data.get("status")
            )
        except Exception:
            return ToolResult.success(str(body), http_status=data.get("status"))

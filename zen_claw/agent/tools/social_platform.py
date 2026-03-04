"""Social platform REST connector tools."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult

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
        },
        "required": ["base_url", "endpoint", "payload", "auth_header"],
    }

    def __init__(self, proxy_url: str = _DEFAULT_PROXY_URL, timeout_sec: float = 20.0):
        self.proxy_url = proxy_url
        self.timeout_sec = float(timeout_sec)

    async def execute(
        self,
        base_url: str,
        endpoint: str,
        payload: dict,
        auth_header: str,
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
        req = {
            "url": target_url,
            "method": "POST",
            "headers": {"Authorization": auth_header, "Content-Type": "application/json"},
            "body": json.dumps(payload, ensure_ascii=False),
        }
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if trace_id:
            headers["X-Trace-Id"] = trace_id
        try:
            async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
                resp = await client.post(self.proxy_url, headers=headers, json=req)
        except httpx.TimeoutException as exc:
            return ToolResult.failure(ToolErrorKind.RETRYABLE, str(exc), code="social_post_timeout")
        except httpx.RequestError as exc:
            return ToolResult.failure(ToolErrorKind.RETRYABLE, str(exc), code="social_post_proxy_unreachable")
        try:
            data = resp.json()
        except Exception:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME, "Proxy returned non-JSON response", code="social_post_invalid_response"
            )
        if resp.status_code >= 400 or not bool(data.get("ok", True)):
            msg = str(data.get("error") or f"HTTP {resp.status_code}")
            code = str(data.get("error_code") or "social_post_failed")
            kind = ToolErrorKind.PERMISSION if resp.status_code in {401, 403} else ToolErrorKind.RUNTIME
            return ToolResult.failure(kind, msg, code=code)
        body = data.get("body") or ""
        try:
            parsed = json.loads(body) if isinstance(body, str) else body
            return ToolResult.success(json.dumps(parsed, ensure_ascii=False), http_status=data.get("status"))
        except Exception:
            return ToolResult.success(str(body), http_status=data.get("status"))


class SocialPlatformLikeTool(Tool):
    """Like/upvote a post on a social platform (via net-proxy)."""

    name = "social_platform_like"
    description = "Upvote / like a post on a social platform REST API (via net-proxy)."
    parameters = {
        "type": "object",
        "properties": {
            "base_url": {"type": "string", "description": "Base URL of the social platform API."},
            "post_id": {"type": "string", "description": "ID of the post to upvote."},
            "auth_header": {"type": "string", "description": "Authorization header value (e.g. 'Bearer <token>')."},
        },
        "required": ["base_url", "post_id", "auth_header"],
    }

    def __init__(self, proxy_url: str = _DEFAULT_PROXY_URL, timeout_sec: float = 15.0):
        self.proxy_url = proxy_url
        self.timeout_sec = float(timeout_sec)

    async def execute(
        self,
        base_url: str,
        post_id: str,
        auth_header: str,
        **kwargs: Any,
    ) -> ToolResult:
        valid, err = _validate_base_url(base_url)
        if not valid:
            return ToolResult.failure(ToolErrorKind.PARAMETER, err, code="invalid_base_url")

        pid = str(post_id or "").strip()
        if not pid:
            return ToolResult.failure(ToolErrorKind.PARAMETER, "post_id is required", code="missing_post_id")

        target_url = str(base_url).rstrip("/") + f"/api/posts/{pid}/upvote"
        trace_id = str(kwargs.get("trace_id") or "")
        req = {
            "url": target_url,
            "method": "POST",
            "headers": {"Authorization": auth_header, "Content-Type": "application/json"},
            "body": "{}",
        }
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if trace_id:
            headers["X-Trace-Id"] = trace_id
        try:
            async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
                resp = await client.post(self.proxy_url, headers=headers, json=req)
        except httpx.TimeoutException as exc:
            return ToolResult.failure(ToolErrorKind.RETRYABLE, str(exc), code="social_like_timeout")
        except httpx.RequestError as exc:
            return ToolResult.failure(ToolErrorKind.RETRYABLE, str(exc), code="social_like_proxy_unreachable")
        try:
            data = resp.json()
        except Exception:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME, "Proxy returned non-JSON response", code="social_like_invalid_response"
            )
        if resp.status_code >= 400 or not bool(data.get("ok", True)):
            msg = str(data.get("error") or f"HTTP {resp.status_code}")
            code = str(data.get("error_code") or "social_like_failed")
            kind = ToolErrorKind.PERMISSION if resp.status_code in {401, 403} else ToolErrorKind.RUNTIME
            return ToolResult.failure(kind, msg, code=code)
        body = data.get("body") or "{}"
        try:
            parsed = json.loads(body) if isinstance(body, str) else body
            return ToolResult.success(json.dumps(parsed, ensure_ascii=False), http_status=data.get("status"))
        except Exception:
            return ToolResult.success(str(body), http_status=data.get("status"))
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
            return ToolResult.failure(ToolErrorKind.RETRYABLE, str(exc), code="social_get_proxy_unreachable")
        try:
            data = resp.json()
        except Exception:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME, "Proxy returned non-JSON response", code="social_get_invalid_response"
            )
        if resp.status_code >= 400 or not bool(data.get("ok", True)):
            msg = str(data.get("error") or f"HTTP {resp.status_code}")
            code = str(data.get("error_code") or "social_get_failed")
            kind = ToolErrorKind.PERMISSION if resp.status_code in {401, 403} else ToolErrorKind.RUNTIME
            return ToolResult.failure(kind, msg, code=code)
        body = data.get("body") or ""
        try:
            parsed = json.loads(body) if isinstance(body, str) else body
            return ToolResult.success(json.dumps(parsed, ensure_ascii=False), http_status=data.get("status"))
        except Exception:
            return ToolResult.success(str(body), http_status=data.get("status"))


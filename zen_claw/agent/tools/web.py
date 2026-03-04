"""Web tools: web_search and web_fetch."""

import html
import ipaddress
import json
import os
import re
import socket
from typing import Any
from urllib.parse import urlparse

import httpx

from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult

# Shared constants
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) AppleWebKit/537.36"
MAX_REDIRECTS = 5  # Limit redirects to prevent DoS attacks

# Private / reserved IP networks blocked to prevent SSRF
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),      # loopback
    ipaddress.ip_network("10.0.0.0/8"),        # RFC 1918
    ipaddress.ip_network("172.16.0.0/12"),     # RFC 1918
    ipaddress.ip_network("192.168.0.0/16"),    # RFC 1918
    ipaddress.ip_network("169.254.0.0/16"),    # link-local / AWS metadata
    ipaddress.ip_network("100.64.0.0/10"),     # CGNAT
    ipaddress.ip_network("0.0.0.0/8"),         # this-network
    ipaddress.ip_network("::1/128"),            # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),           # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),          # IPv6 link-local
]


def _is_private_host(host: str) -> bool:
    """Return True if the host resolves to a private/reserved IP address."""
    try:
        addr = ipaddress.ip_address(host)
        return any(addr in net for net in _BLOCKED_NETWORKS)
    except ValueError:
        pass
    # Not a bare IP — try DNS resolution
    try:
        infos = socket.getaddrinfo(host, None)
        for _, _, _, _, sockaddr in infos:
            ip_str = sockaddr[0]
            try:
                addr = ipaddress.ip_address(ip_str)
                if any(addr in net for net in _BLOCKED_NETWORKS):
                    return True
            except ValueError:
                continue
    except OSError:
        pass
    return False


def _strip_tags(text: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r'<script[\s\S]*?</script>', '', text, flags=re.I)
    text = re.sub(r'<style[\s\S]*?</style>', '', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    return html.unescape(text).strip()


def _normalize(text: str) -> str:
    """Normalize whitespace."""
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def _validate_url(url: str) -> tuple[bool, str]:
    """Validate URL: must be http(s), public domain, non-private IP."""
    try:
        p = urlparse(url)
        if p.scheme not in ('http', 'https'):
            return False, f"Only http/https allowed, got '{p.scheme or 'none'}'"
        if not p.netloc:
            return False, "Missing domain"
        # Strip port from host for IP check
        host = p.hostname or ""
        if not host:
            return False, "Missing host"
        if _is_private_host(host):
            return False, f"Access to private/reserved address '{host}' is not allowed"
        return True, ""
    except Exception as e:
        return False, str(e)


def _build_proxy_healthz_url(proxy_url: str, suffix: str) -> str:
    p = urlparse(proxy_url)
    path = p.path
    if path.endswith(suffix):
        path = path[: -len(suffix)] + "/healthz"
    else:
        path = "/healthz"
    return p._replace(path=path, params="", query="", fragment="").geturl()


class WebSearchTool(Tool):
    """Search the web using Brave Search API."""

    name = "web_search"
    description = "Search the web. Returns titles, URLs, and snippets."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "count": {"type": "integer", "description": "Results (1-10)", "minimum": 1, "maximum": 10}
        },
        "required": ["query"]
    }

    def __init__(
        self,
        api_key: str | None = None,
        max_results: int = 5,
        mode: str = "local",
        proxy_url: str = "http://127.0.0.1:4499/v1/search",
        proxy_healthcheck: bool = False,
        proxy_fallback_to_local: bool = False,
    ):
        self.api_key = api_key or os.environ.get("BRAVE_API_KEY", "")
        self.max_results = max_results
        self.mode = mode
        self.proxy_url = proxy_url
        self.proxy_healthcheck = proxy_healthcheck
        self.proxy_fallback_to_local = proxy_fallback_to_local

    async def execute(self, query: str, count: int | None = None, **kwargs: Any) -> ToolResult:
        trace_id = str(kwargs.get("trace_id") or "")
        if self.mode == "proxy":
            return await self._search_via_proxy(query=query, count=count, trace_id=trace_id)
        return await self._search_local(query=query, count=count)

    async def _search_local(self, query: str, count: int | None = None) -> ToolResult:
        if not self.api_key:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                "BRAVE_API_KEY not configured",
                code="brave_api_key_missing",
            )

        try:
            n = min(max(count or self.max_results, 1), 10)
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": query, "count": n},
                    headers={"Accept": "application/json", "X-Subscription-Token": self.api_key},
                    timeout=10.0
                )
                r.raise_for_status()

            results = r.json().get("web", {}).get("results", [])
            if not results:
                return ToolResult.success(f"No results for: {query}")

            lines = [f"Results for: {query}\n"]
            for i, item in enumerate(results[:n], 1):
                lines.append(f"{i}. {item.get('title', '')}\n   {item.get('url', '')}")
                if desc := item.get("description"):
                    lines.append(f"   {desc}")
            return ToolResult.success("\n".join(lines))
        except httpx.TimeoutException as e:
            return ToolResult.failure(
                ToolErrorKind.RETRYABLE,
                str(e),
                code="web_search_timeout",
            )
        except httpx.RequestError as e:
            return ToolResult.failure(
                ToolErrorKind.RETRYABLE,
                str(e),
                code="web_search_request_failed",
            )
        except Exception as e:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                str(e),
                code="web_search_failed",
            )

    async def _search_via_proxy(self, query: str, count: int | None, trace_id: str) -> ToolResult:
        headers = {"Content-Type": "application/json"}
        if trace_id:
            headers["X-Trace-Id"] = trace_id

        payload = {
            "query": query,
            "count": min(max(count or self.max_results, 1), 10),
            "api_key": self.api_key,
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                if self.proxy_healthcheck:
                    health = await client.get(_build_proxy_healthz_url(self.proxy_url, "/v1/search"))
                    if health.status_code >= 400:
                        if self.proxy_fallback_to_local:
                            return await self._search_local(query=query, count=count)
                        return ToolResult.failure(
                            ToolErrorKind.RETRYABLE,
                            f"Net proxy health check failed with HTTP {health.status_code}",
                            code="web_search_proxy_unhealthy",
                        )

                response = await client.post(self.proxy_url, headers=headers, json=payload)
        except httpx.TimeoutException as e:
            if self.proxy_fallback_to_local:
                return await self._search_local(query=query, count=count)
            return ToolResult.failure(
                ToolErrorKind.RETRYABLE,
                str(e),
                code="web_search_proxy_timeout",
            )
        except httpx.RequestError as e:
            if self.proxy_fallback_to_local:
                return await self._search_local(query=query, count=count)
            return ToolResult.failure(
                ToolErrorKind.RETRYABLE,
                str(e),
                code="web_search_proxy_unreachable",
            )

        try:
            data = response.json()
        except Exception:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                "Net proxy returned invalid JSON",
                code="web_search_proxy_invalid_response",
            )

        if response.status_code >= 400 or not bool(data.get("ok")):
            code = str(data.get("error_code") or "web_search_proxy_failed")
            msg = str(data.get("error") or f"HTTP {response.status_code}")
            kind = ToolErrorKind.PERMISSION if response.status_code == 403 else ToolErrorKind.RUNTIME
            return ToolResult.failure(kind, msg, code=code, http_status=response.status_code)

        results = data.get("results") or []
        if not results:
            return ToolResult.success(f"No results for: {query}")

        lines = [f"Results for: {query}\n"]
        for i, item in enumerate(results, 1):
            lines.append(f"{i}. {item.get('title', '')}\n   {item.get('url', '')}")
            if desc := item.get("description"):
                lines.append(f"   {desc}")
        return ToolResult.success("\n".join(lines))


class WebFetchTool(Tool):
    """Fetch and extract content from a URL using Readability."""

    name = "web_fetch"
    description = "Fetch URL and extract readable content (HTML or markdown/text)."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "extractMode": {"type": "string", "enum": ["markdown", "text"], "default": "markdown"},
            "maxChars": {"type": "integer", "minimum": 100}
        },
        "required": ["url"]
    }

    def __init__(
        self,
        max_chars: int = 50000,
        mode: str = "local",
        proxy_url: str = "http://127.0.0.1:4499/v1/fetch",
        proxy_healthcheck: bool = False,
        proxy_fallback_to_local: bool = False,
    ):
        self.max_chars = max_chars
        self.mode = mode
        self.proxy_url = proxy_url
        self.proxy_healthcheck = proxy_healthcheck
        self.proxy_fallback_to_local = proxy_fallback_to_local

    async def execute(
        self,
        url: str,
        extractMode: str = "markdown",
        maxChars: int | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        max_chars = maxChars or self.max_chars
        trace_id = str(kwargs.get("trace_id") or "")

        # Validate URL before fetching
        is_valid, error_msg = _validate_url(url)
        if not is_valid:
            return ToolResult.failure(
                ToolErrorKind.PARAMETER,
                f"URL validation failed: {error_msg}",
                code="url_invalid",
            )

        if self.mode == "proxy":
            return await self._fetch_via_proxy(
                url=url,
                extract_mode=extractMode,
                max_chars=max_chars,
                trace_id=trace_id,
            )

        return await self._fetch_local(url=url, extract_mode=extractMode, max_chars=max_chars)

    async def _fetch_local(self, url: str, extract_mode: str, max_chars: int) -> ToolResult:
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                max_redirects=MAX_REDIRECTS,
                timeout=30.0
            ) as client:
                r = await client.get(url, headers={"User-Agent": USER_AGENT})
                r.raise_for_status()

            ctype = r.headers.get("content-type", "")

            # JSON
            if "application/json" in ctype:
                text, extractor = json.dumps(r.json(), indent=2), "json"
            # HTML
            elif "text/html" in ctype or r.text[:256].lower().startswith(("<!doctype", "<html")):
                from readability import Document
                doc = Document(r.text)
                content = self._to_markdown(doc.summary()) if extract_mode == "markdown" else _strip_tags(doc.summary())
                text = f"# {doc.title()}\n\n{content}" if doc.title() else content
                extractor = "readability"
            else:
                text, extractor = r.text, "raw"

            truncated = len(text) > max_chars
            if truncated:
                text = text[:max_chars]

            return ToolResult.success(
                json.dumps(
                    {
                        "url": url,
                        "finalUrl": str(r.url),
                        "status": r.status_code,
                        "extractor": extractor,
                        "truncated": truncated,
                        "length": len(text),
                        "text": text,
                    }
                )
            )
        except httpx.TimeoutException as e:
            return ToolResult.failure(
                ToolErrorKind.RETRYABLE,
                str(e),
                code="web_fetch_timeout",
            )
        except httpx.RequestError as e:
            return ToolResult.failure(
                ToolErrorKind.RETRYABLE,
                str(e),
                code="web_fetch_request_failed",
            )
        except Exception as e:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                str(e),
                code="web_fetch_failed",
            )

    async def _fetch_via_proxy(
        self,
        url: str,
        extract_mode: str,
        max_chars: int,
        trace_id: str,
    ) -> ToolResult:
        headers = {"Content-Type": "application/json"}
        if trace_id:
            headers["X-Trace-Id"] = trace_id

        payload = {
            "url": url,
            "max_bytes": max_chars,
        }

        try:
            async with httpx.AsyncClient(timeout=35.0) as client:
                if self.proxy_healthcheck:
                    health_url = _build_proxy_healthz_url(self.proxy_url, "/v1/fetch")
                    health = await client.get(health_url)
                    if health.status_code >= 400:
                        if self.proxy_fallback_to_local:
                            return await self._fetch_local(url=url, extract_mode=extract_mode, max_chars=max_chars)
                        return ToolResult.failure(
                            ToolErrorKind.RETRYABLE,
                            f"Net proxy health check failed with HTTP {health.status_code}",
                            code="web_fetch_proxy_unhealthy",
                        )

                response = await client.post(self.proxy_url, headers=headers, json=payload)
        except httpx.TimeoutException as e:
            if self.proxy_fallback_to_local:
                return await self._fetch_local(url=url, extract_mode=extract_mode, max_chars=max_chars)
            return ToolResult.failure(
                ToolErrorKind.RETRYABLE,
                str(e),
                code="web_fetch_proxy_timeout",
            )
        except httpx.RequestError as e:
            if self.proxy_fallback_to_local:
                return await self._fetch_local(url=url, extract_mode=extract_mode, max_chars=max_chars)
            return ToolResult.failure(
                ToolErrorKind.RETRYABLE,
                str(e),
                code="web_fetch_proxy_unreachable",
            )

        try:
            data = response.json()
        except Exception:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                "Net proxy returned invalid JSON",
                code="web_fetch_proxy_invalid_response",
            )

        if response.status_code >= 400:
            code = str(data.get("error_code") or "web_fetch_proxy_error")
            msg = str(data.get("error") or f"HTTP {response.status_code}")
            kind = ToolErrorKind.PERMISSION if response.status_code == 403 else ToolErrorKind.RUNTIME
            return ToolResult.failure(kind, msg, code=code, http_status=response.status_code)

        if not bool(data.get("ok")):
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                str(data.get("error") or "proxy fetch failed"),
                code=str(data.get("error_code") or "web_fetch_proxy_failed"),
            )

        text = str(data.get("body") or "")
        ctype = str(data.get("content_type") or "").lower()
        truncated = bool(data.get("truncated") or False)
        extractor = "proxy_raw"

        if "text/html" in ctype or text[:256].lower().startswith(("<!doctype", "<html")):
            try:
                from readability import Document
                doc = Document(text)
                content = self._to_markdown(doc.summary()) if extract_mode == "markdown" else _strip_tags(doc.summary())
                text = f"# {doc.title()}\n\n{content}" if doc.title() else content
                extractor = "proxy_readability"
            except Exception:
                pass

        if len(text) > max_chars:
            text = text[:max_chars]
            truncated = True

        return ToolResult.success(
            json.dumps(
                {
                    "url": url,
                    "finalUrl": str(data.get("final_url") or url),
                    "status": int(data.get("status") or 200),
                    "extractor": extractor,
                    "truncated": truncated,
                    "length": len(text),
                    "text": text,
                }
            )
        )


    def _to_markdown(self, html: str) -> str:
        """Convert HTML to markdown."""
        # Convert links, headings, lists before stripping tags
        text = re.sub(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>',
                      lambda m: f'[{_strip_tags(m[2])}]({m[1]})', html, flags=re.I)
        text = re.sub(r'<h([1-6])[^>]*>([\s\S]*?)</h\1>',
                      lambda m: f'\n{"#" * int(m[1])} {_strip_tags(m[2])}\n', text, flags=re.I)
        text = re.sub(r'<li[^>]*>([\s\S]*?)</li>', lambda m: f'\n- {_strip_tags(m[1])}', text, flags=re.I)
        text = re.sub(r'</(p|div|section|article)>', '\n\n', text, flags=re.I)
        text = re.sub(r'<(br|hr)\s*/?>', '\n', text, flags=re.I)
        return _normalize(_strip_tags(text))



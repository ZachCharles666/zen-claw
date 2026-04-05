from zen_claw.agent.tools.result import ToolErrorKind
from zen_claw.agent.tools.web import WebSearchTool
from zen_claw.security_context import build_security_context


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(
        self,
        response: _FakeResponse | None = None,
        health_response: _FakeResponse | None = None,
    ):
        self._response = response or _FakeResponse(200, {"ok": True, "results": []})
        self._health = health_response or _FakeResponse(200, {"ok": True})
        self.last_headers: dict[str, str] = {}

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def get(self, url: str) -> _FakeResponse:
        return self._health

    async def post(self, url: str, headers: dict, json: dict | None = None, content: bytes | None = None) -> _FakeResponse:
        self.last_headers = headers
        return self._response


def _security_context(trace_id: str) -> dict:
    return build_security_context(
        trace_id=trace_id,
        channel="cli",
        sender_id="tester",
        chat_id="direct",
        tenant_id="default",
        workspace_id="workspace",
        agent_profile="default",
        role="admin",
        trust_level="trusted_local",
        origin_surface="cli",
        policy_snapshot={"production_hardening": True, "policy_snapshot_hash": "policy-test"},
    )


async def test_web_search_proxy_success(monkeypatch) -> None:
    payload = {
        "ok": True,
        "results": [
            {"title": "A", "url": "https://a.com", "description": "desc"},
        ],
    }
    fake = _FakeClient(response=_FakeResponse(200, payload))
    monkeypatch.setattr("zen_claw.agent.tools.web.httpx.AsyncClient", lambda timeout: fake)

    tool = WebSearchTool(
        api_key="k",
        mode="proxy",
        proxy_url="http://127.0.0.1:4499/v1/search",
        proxy_approval_mode="token",
    )
    result = await tool.execute("hello", trace_id="trace-search-success", security_context=_security_context("trace-search-success"))
    assert result.ok is True
    assert "Results for: hello" in result.content
    assert "https://a.com" in result.content


async def test_web_search_proxy_permission_error(monkeypatch) -> None:
    fake = _FakeClient(
        response=_FakeResponse(
            403,
            {"ok": False, "error_code": "domain_denied", "error": "denied"},
        )
    )
    monkeypatch.setattr("zen_claw.agent.tools.web.httpx.AsyncClient", lambda timeout: fake)

    tool = WebSearchTool(
        api_key="k",
        mode="proxy",
        proxy_url="http://127.0.0.1:4499/v1/search",
        proxy_approval_mode="token",
    )
    result = await tool.execute("hello", trace_id="trace-search-perm", security_context=_security_context("trace-search-perm"))
    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.PERMISSION
    assert result.error.code == "domain_denied"


async def test_web_search_proxy_passes_trace_header(monkeypatch) -> None:
    fake = _FakeClient(response=_FakeResponse(200, {"ok": True, "results": []}))
    monkeypatch.setattr("zen_claw.agent.tools.web.httpx.AsyncClient", lambda timeout: fake)

    tool = WebSearchTool(
        api_key="k",
        mode="proxy",
        proxy_url="http://127.0.0.1:4499/v1/search",
        proxy_approval_mode="token",
    )
    result = await tool.execute("hello", trace_id="trace-search-1", security_context=_security_context("trace-search-1"))
    assert result.ok is True
    assert fake.last_headers.get("X-Trace-Id") == "trace-search-1"


async def test_web_search_proxy_healthcheck_failed(monkeypatch) -> None:
    fake = _FakeClient(
        response=_FakeResponse(200, {"ok": True, "results": []}),
        health_response=_FakeResponse(503, {"ok": False}),
    )
    monkeypatch.setattr("zen_claw.agent.tools.web.httpx.AsyncClient", lambda timeout: fake)

    tool = WebSearchTool(
        api_key="k",
        mode="proxy",
        proxy_url="http://127.0.0.1:4499/v1/search",
        proxy_approval_mode="token",
        proxy_healthcheck=True,
    )
    result = await tool.execute("hello", trace_id="trace-search-health", security_context=_security_context("trace-search-health"))
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "web_search_proxy_unhealthy"


async def test_web_search_proxy_healthcheck_fallback_to_local(monkeypatch) -> None:
    fake = _FakeClient(
        response=_FakeResponse(200, {"ok": True, "results": []}),
        health_response=_FakeResponse(503, {"ok": False}),
    )
    monkeypatch.setattr("zen_claw.agent.tools.web.httpx.AsyncClient", lambda timeout: fake)

    async def fake_local(query: str, count: int | None = None):
        from zen_claw.agent.tools.result import ToolResult

        return ToolResult.success("local-search-ok")

    tool = WebSearchTool(
        api_key="k",
        mode="proxy",
        proxy_url="http://127.0.0.1:4499/v1/search",
        proxy_approval_mode="token",
        proxy_healthcheck=True,
        proxy_fallback_to_local=True,
    )
    monkeypatch.setattr(tool, "_search_local", fake_local)
    result = await tool.execute("hello", trace_id="trace-search-fallback", security_context=_security_context("trace-search-fallback"))
    assert result.ok is True
    assert result.content == "local-search-ok"

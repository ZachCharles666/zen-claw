import json

from zen_claw.agent.tools.result import ToolErrorKind
from zen_claw.agent.tools.web import WebFetchTool


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
        self._response = response or _FakeResponse(200, {"ok": True, "status": 200, "body": "ok"})
        self._health = health_response or _FakeResponse(200, {"ok": True})
        self.last_headers: dict[str, str] = {}

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def get(self, url: str) -> _FakeResponse:
        return self._health

    async def post(self, url: str, headers: dict, json: dict) -> _FakeResponse:
        self.last_headers = headers
        return self._response


async def test_web_fetch_proxy_success(monkeypatch) -> None:
    payload = {
        "ok": True,
        "status": 200,
        "final_url": "https://example.com",
        "truncated": False,
        "body": "hello from proxy",
    }
    fake = _FakeClient(response=_FakeResponse(200, payload))
    monkeypatch.setattr("zen_claw.agent.tools.web.httpx.AsyncClient", lambda timeout: fake)

    tool = WebFetchTool(mode="proxy", proxy_url="http://127.0.0.1:4499/v1/fetch")
    result = await tool.execute("https://example.com")
    assert result.ok is True
    data = json.loads(result.content)
    assert data["extractor"] == "proxy_raw"
    assert data["text"] == "hello from proxy"


async def test_web_fetch_proxy_denied(monkeypatch) -> None:
    fake = _FakeClient(
        response=_FakeResponse(
            403,
            {"ok": False, "error_code": "domain_denied", "error": "denied"},
        )
    )
    monkeypatch.setattr("zen_claw.agent.tools.web.httpx.AsyncClient", lambda timeout: fake)

    tool = WebFetchTool(mode="proxy", proxy_url="http://127.0.0.1:4499/v1/fetch")
    result = await tool.execute("https://example.com")
    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.PERMISSION
    assert result.error.code == "domain_denied"


async def test_web_fetch_proxy_passes_trace_header(monkeypatch) -> None:
    fake = _FakeClient(response=_FakeResponse(200, {"ok": True, "status": 200, "body": "ok"}))
    monkeypatch.setattr("zen_claw.agent.tools.web.httpx.AsyncClient", lambda timeout: fake)

    tool = WebFetchTool(mode="proxy", proxy_url="http://127.0.0.1:4499/v1/fetch")
    result = await tool.execute("https://example.com", trace_id="trace-web-1")
    assert result.ok is True
    assert fake.last_headers.get("X-Trace-Id") == "trace-web-1"

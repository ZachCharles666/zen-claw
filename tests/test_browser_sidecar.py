import json

from zen_claw.agent.tools.browser import (
    BrowserClickTool,
    BrowserExtractTool,
    BrowserOpenTool,
    BrowserTypeTool,
)
from zen_claw.agent.tools.result import ToolErrorKind


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
        self._response = response or _FakeResponse(200, {"ok": True, "session_id": "s1"})
        self._health = health_response or _FakeResponse(200, {"ok": True})
        self.last_headers: dict[str, str] = {}
        self.last_json: dict = {}

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def get(self, url: str) -> _FakeResponse:
        return self._health

    async def post(self, url: str, headers: dict, json: dict) -> _FakeResponse:
        self.last_headers = headers
        self.last_json = json
        return self._response


async def test_browser_open_sidecar_success(monkeypatch) -> None:
    fake = _FakeClient(response=_FakeResponse(200, {"ok": True, "session_id": "s-open"}))
    monkeypatch.setattr("zen_claw.agent.tools.browser.httpx.AsyncClient", lambda **kwargs: fake)

    tool = BrowserOpenTool(mode="sidecar", sidecar_url="http://127.0.0.1:4500/v1/browser")
    result = await tool.execute(url="https://example.com")
    assert result.ok is True
    data = json.loads(result.content)
    assert data["session_id"] == "s-open"
    assert fake.last_json["action"] == "open"


async def test_browser_sidecar_trace_header(monkeypatch) -> None:
    fake = _FakeClient(response=_FakeResponse(200, {"ok": True}))
    monkeypatch.setattr("zen_claw.agent.tools.browser.httpx.AsyncClient", lambda **kwargs: fake)

    tool = BrowserExtractTool(mode="sidecar", sidecar_url="http://127.0.0.1:4500/v1/browser")
    result = await tool.execute(trace_id="trace-browser-1")
    assert result.ok is True
    assert fake.last_headers.get("X-Trace-Id") == "trace-browser-1"


async def test_browser_sends_approval_token_header(monkeypatch) -> None:
    """When sidecar_approval_token is set, X-Approval-Token must appear in every request."""
    fake = _FakeClient(response=_FakeResponse(200, {"ok": True, "session_id": "s1"}))
    monkeypatch.setattr("zen_claw.agent.tools.browser.httpx.AsyncClient", lambda **kwargs: fake)

    tool = BrowserOpenTool(
        mode="sidecar",
        sidecar_url="http://127.0.0.1:4500/v1/browser",
        sidecar_approval_token="super-secret",
    )
    result = await tool.execute(url="https://example.com")
    assert result.ok is True
    assert fake.last_headers.get("X-Approval-Token") == "super-secret"


async def test_browser_no_token_header_when_token_empty(monkeypatch) -> None:
    """When sidecar_approval_token is empty (default), X-Approval-Token must NOT be sent."""
    fake = _FakeClient(response=_FakeResponse(200, {"ok": True, "session_id": "s1"}))
    monkeypatch.setattr("zen_claw.agent.tools.browser.httpx.AsyncClient", lambda **kwargs: fake)

    tool = BrowserOpenTool(mode="sidecar", sidecar_url="http://127.0.0.1:4500/v1/browser")
    result = await tool.execute(url="https://example.com")
    assert result.ok is True
    assert "X-Approval-Token" not in fake.last_headers


async def test_browser_mode_off_denied() -> None:
    tool = BrowserOpenTool(mode="off")
    result = await tool.execute(url="https://example.com")
    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.PERMISSION
    assert result.error.code == "browser_disabled"


async def test_browser_click_sidecar_success(monkeypatch) -> None:
    fake = _FakeClient(response=_FakeResponse(200, {"ok": True, "session_id": "s1"}))
    monkeypatch.setattr("zen_claw.agent.tools.browser.httpx.AsyncClient", lambda **kwargs: fake)

    tool = BrowserClickTool(mode="sidecar", sidecar_url="http://127.0.0.1:4500/v1/browser")
    result = await tool.execute(sessionId="s1", selector="#submit")
    assert result.ok is True
    assert fake.last_json["action"] == "click"
    assert fake.last_json["payload"]["selector"] == "#submit"


async def test_browser_type_sidecar_success(monkeypatch) -> None:
    fake = _FakeClient(response=_FakeResponse(200, {"ok": True, "session_id": "s1"}))
    monkeypatch.setattr("zen_claw.agent.tools.browser.httpx.AsyncClient", lambda **kwargs: fake)

    tool = BrowserTypeTool(mode="sidecar", sidecar_url="http://127.0.0.1:4500/v1/browser")
    result = await tool.execute(sessionId="s1", selector="#q", text="zen-claw", clear=True)
    assert result.ok is True
    assert fake.last_json["action"] == "type"
    assert fake.last_json["payload"]["text"] == "zen-claw"


async def test_browser_open_max_steps_override(monkeypatch) -> None:
    fake = _FakeClient(response=_FakeResponse(200, {"ok": True, "session_id": "s-open"}))
    monkeypatch.setattr("zen_claw.agent.tools.browser.httpx.AsyncClient", lambda **kwargs: fake)

    tool = BrowserOpenTool(
        mode="sidecar", sidecar_url="http://127.0.0.1:4500/v1/browser", max_steps=20
    )
    result = await tool.execute(url="https://example.com", maxSteps=50)
    assert result.ok is True
    assert fake.last_json["policy"]["max_steps"] == 50


async def test_browser_open_max_steps_override_minimum(monkeypatch) -> None:
    fake = _FakeClient(response=_FakeResponse(200, {"ok": True, "session_id": "s-open"}))
    monkeypatch.setattr("zen_claw.agent.tools.browser.httpx.AsyncClient", lambda **kwargs: fake)

    tool = BrowserOpenTool(
        mode="sidecar", sidecar_url="http://127.0.0.1:4500/v1/browser", max_steps=20
    )
    result = await tool.execute(url="https://example.com", maxSteps=0)
    assert result.ok is True
    assert fake.last_json["policy"]["max_steps"] == 1

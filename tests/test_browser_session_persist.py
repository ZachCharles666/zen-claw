import asyncio
import json
from typing import Any

from zen_claw.agent.tools.browser import BrowserLoadSessionTool, BrowserSaveSessionTool
from zen_claw.agent.tools.result import ToolErrorKind
from zen_claw.security_context import build_security_context


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    def __init__(self, response: _FakeResponse):
        self._response = response
        self.last_json: dict[str, Any] = {}

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(
        self,
        url: str,
        headers: dict[str, str],
        json: dict[str, Any] | None = None,
        content: bytes | None = None,
    ) -> _FakeResponse:
        self.last_json = json or {}
        return self._response


def _security_context(trace_id: str) -> dict[str, Any]:
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
        policy_snapshot={"production_hardening": True},
    )


def test_browser_save_session_sends_expected_request(monkeypatch) -> None:
    fake = _FakeClient(
        _FakeResponse(200, {"ok": True, "action": "save_session", "path": "/tmp/s1.json"})
    )
    monkeypatch.setattr("zen_claw.agent.tools.browser.httpx.AsyncClient", lambda **kwargs: fake)
    tool = BrowserSaveSessionTool(mode="sidecar", sidecar_approval_mode="token")
    result = asyncio.run(
        tool.execute(
            sessionId="abc123",
            trace_id="trace-save-session",
            security_context=_security_context("trace-save-session"),
        )
    )
    assert result.ok is True
    assert fake.last_json["action"] == "save_session"
    assert fake.last_json["payload"]["session_id"] == "abc123"


def test_browser_save_session_parses_response(monkeypatch) -> None:
    fake = _FakeClient(
        _FakeResponse(200, {"ok": True, "action": "save_session", "path": "/tmp/s1.json"})
    )
    monkeypatch.setattr("zen_claw.agent.tools.browser.httpx.AsyncClient", lambda **kwargs: fake)
    tool = BrowserSaveSessionTool(mode="sidecar", sidecar_approval_mode="token")
    result = asyncio.run(
        tool.execute(
            sessionId="abc123",
            trace_id="trace-save-session-2",
            security_context=_security_context("trace-save-session-2"),
        )
    )
    assert result.ok is True
    assert "/tmp/s1.json" in str(result.content)


def test_browser_load_session_sends_expected_request(monkeypatch) -> None:
    fake = _FakeClient(
        _FakeResponse(
            200,
            {
                "ok": True,
                "action": "load_session",
                "session_id": "new-s",
                "state_file": "/tmp/s1.json",
            },
        )
    )
    monkeypatch.setattr("zen_claw.agent.tools.browser.httpx.AsyncClient", lambda **kwargs: fake)
    tool = BrowserLoadSessionTool(mode="sidecar", sidecar_approval_mode="token")
    result = asyncio.run(
        tool.execute(
            sessionId="abc123",
            trace_id="trace-load-session",
            security_context=_security_context("trace-load-session"),
        )
    )
    assert result.ok is True
    assert fake.last_json["action"] == "load_session"
    assert fake.last_json["payload"]["session_id"] == "abc123"


def test_browser_load_session_prefers_state_file_when_both_provided(monkeypatch) -> None:
    fake = _FakeClient(
        _FakeResponse(
            200,
            {
                "ok": True,
                "action": "load_session",
                "session_id": "new-s",
                "state_file": "/tmp/s1.json",
            },
        )
    )
    monkeypatch.setattr("zen_claw.agent.tools.browser.httpx.AsyncClient", lambda **kwargs: fake)
    tool = BrowserLoadSessionTool(mode="sidecar", sidecar_approval_mode="token")
    result = asyncio.run(
        tool.execute(
            sessionId="abc123",
            stateFile="/tmp/custom.json",
            trace_id="trace-load-session-2",
            security_context=_security_context("trace-load-session-2"),
        )
    )
    assert result.ok is True
    assert fake.last_json["payload"]["state_file"] == "/tmp/custom.json"
    assert fake.last_json["payload"]["session_id"] == "abc123"


def test_browser_load_session_missing_args_returns_parameter_error() -> None:
    tool = BrowserLoadSessionTool(mode="sidecar")
    result = asyncio.run(tool.execute())
    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.PARAMETER
    assert result.error.code == "browser_load_session_missing_args"


def test_browser_save_session_mode_off_returns_permission_error() -> None:
    tool = BrowserSaveSessionTool(mode="off")
    result = asyncio.run(tool.execute(sessionId="abc123"))
    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.PERMISSION


def test_browser_load_session_result_contains_new_session_id(monkeypatch) -> None:
    fake = _FakeClient(
        _FakeResponse(
            200,
            {
                "ok": True,
                "action": "load_session",
                "session_id": "brand-new",
                "state_file": "/tmp/old.json",
            },
        )
    )
    monkeypatch.setattr("zen_claw.agent.tools.browser.httpx.AsyncClient", lambda **kwargs: fake)
    tool = BrowserLoadSessionTool(mode="sidecar", sidecar_approval_mode="token")
    result = asyncio.run(
        tool.execute(
            sessionId="old",
            trace_id="trace-load-session-3",
            security_context=_security_context("trace-load-session-3"),
        )
    )
    assert result.ok is True
    data = json.loads(result.content)
    assert data["session_id"] == "brand-new"

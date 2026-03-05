"""Tests for API gateway and web chat endpoints."""

from __future__ import annotations

import json

import pytest

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover
    TestClient = None

from zen_claw.bus.events import OutboundMessage
from zen_claw.dashboard.server import api_app, generate_api_key, store_api_key


@pytest.fixture
def client():
    if TestClient is None or api_app is None:
        pytest.skip("fastapi is not available")
    return TestClient(api_app)


@pytest.fixture
def valid_key(tmp_path, monkeypatch):
    from zen_claw.dashboard import server as srv

    monkeypatch.setattr(srv, "_api_keys_file", lambda: tmp_path / "api_keys.json")
    raw, _ = generate_api_key()
    store_api_key(raw)
    return raw


def test_health_is_public(client):
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_info_requires_auth(client):
    resp = client.get("/api/v1/info")
    assert resp.status_code == 401
    assert resp.json()["error"] == "invalid_api_key"


def test_info_with_valid_key(client, valid_key):
    resp = client.get("/api/v1/info", headers={"X-API-Key": valid_key})
    assert resp.status_code == 200
    assert "version" in resp.json()


def test_invoke_no_key_returns_401(client):
    resp = client.post("/api/v1/agent/invoke", json={"message": "hello"})
    assert resp.status_code == 401


def test_invoke_with_key(client, valid_key):
    resp = client.post(
        "/api/v1/agent/invoke", json={"message": "test"}, headers={"X-API-Key": valid_key}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "response" in body
    assert "session_id" in body


def test_env_var_api_key(monkeypatch, client):
    raw = "nc-test-env-key-abc123"
    monkeypatch.setenv("zen_claw_API_KEYS", raw)
    resp = client.get("/api/v1/info", headers={"X-API-Key": raw})
    assert resp.status_code != 401


def test_sse_stream_content_type(client, valid_key):
    with client.stream(
        "POST",
        "/api/v1/agent/invoke/stream",
        headers={"X-API-Key": valid_key},
        json={"message": "stream test"},
    ) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        chunk = next(resp.iter_text())
        assert "data:" in chunk


def test_chat_ws_connects(client):
    with client.websocket_connect("/chat/ws/test-session-001") as ws:
        ws.send_text(json.dumps({"type": "message", "content": "hello"}))
        msg = ws.receive_text()
        data = json.loads(msg)
        assert data["type"] in ("token", "done", "error")


def test_chat_ws_uses_webchat_runtime_when_available(client, monkeypatch):
    from zen_claw.dashboard import server as srv

    class _FakeChannel:
        def __init__(self):
            self.last_content = ""

        async def ingest_user_message(self, **kwargs):
            self.last_content = str(kwargs.get("content", ""))

        async def pop_response(self, _session_id: str, timeout_sec: float = 0.0):
            _ = timeout_sec
            return OutboundMessage(
                channel="webchat", chat_id="s", content=f"bus:{self.last_content}"
            )

    class _Cfg:
        class _Channels:
            class _Webchat:
                token = ""

            webchat = _Webchat()

        channels = _Channels()

    class _FakeRuntime:
        def __init__(self):
            self.cfg = _Cfg()
            self.channel = _FakeChannel()

    async def _fake_runtime():
        return _FakeRuntime()

    monkeypatch.setattr(srv, "_get_webchat_runtime", _fake_runtime)

    with client.websocket_connect("/chat/ws/test-session-002") as ws:
        ws.send_text(json.dumps({"type": "message", "content": "hello-bus"}))
        tokens: list[str] = []
        for _ in range(64):
            data = json.loads(ws.receive_text())
            if data.get("type") == "token":
                tokens.append(str(data.get("content", "")))
                continue
            if data.get("type") == "done":
                break
        assert "".join(tokens).startswith("bus:hello-bus")

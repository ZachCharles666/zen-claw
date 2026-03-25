import asyncio
import hashlib
import hmac
import json
import socket
import time
import urllib
import urllib.error
import urllib.request

from zen_claw.bus.queue import MessageBus
from zen_claw.channels.webhook_trigger import WebhookTriggerChannel
from zen_claw.cli.commands import _gateway_health_payload, _start_gateway_health_server
from zen_claw.config.schema import WebhookTriggerConfig
from zen_claw.dashboard.server import generate_api_key, store_api_key


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(url: str, *, headers: dict[str, str] | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, method="GET")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    last_error: Exception | None = None
    for _ in range(5):
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return int(resp.status), resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return int(exc.code), exc.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError, ConnectionAbortedError) as exc:
            last_error = exc
            time.sleep(0.05)
    raise last_error if last_error is not None else RuntimeError("request failed")


def _post(url: str, payload: dict[str, object], *, api_key: str | None = None) -> tuple[int, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("X-API-Key", api_key)
    last_error: Exception | None = None
    for _ in range(5):
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return int(resp.status), resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return int(exc.code), exc.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError, ConnectionAbortedError) as exc:
            last_error = exc
            time.sleep(0.05)
    raise last_error if last_error is not None else RuntimeError("request failed")


def _post_raw(url: str, body: bytes, headers: dict[str, str] | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, method="POST")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    last_error: Exception | None = None
    for _ in range(5):
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return int(resp.status), resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return int(exc.code), exc.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError, ConnectionAbortedError) as exc:
            last_error = exc
            time.sleep(0.05)
    raise last_error if last_error is not None else RuntimeError("request failed")


def _sign(secret: str, body: bytes, ts: int, nonce: str) -> str:
    payload = f"{ts}.{nonce}.".encode("utf-8") + body
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def test_gateway_health_payload_shape() -> None:
    payload = _gateway_health_payload()
    assert payload["status"] == "ok"
    assert payload["service"] == "zen-claw"
    assert payload["surface"] == "gateway"


def test_gateway_health_server_exposes_api_health() -> None:
    port = _free_port()
    server = _start_gateway_health_server(port=port)
    try:
        status, body = _request(f"http://127.0.0.1:{port}/api/v1/health")
        assert status == 200
        payload = json.loads(body)
        assert payload["status"] == "ok"
        assert payload["surface"] == "gateway"
    finally:
        server.shutdown()
        server.server_close()


def test_gateway_health_server_exposes_healthz() -> None:
    port = _free_port()
    server = _start_gateway_health_server(port=port)
    try:
        status, body = _request(f"http://127.0.0.1:{port}/healthz")
        assert status == 200
        assert body == "ok"
    finally:
        server.shutdown()
        server.server_close()


def test_gateway_invoke_requires_api_key(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)
    port = _free_port()
    server = _start_gateway_health_server(port=port)
    try:
        status, body = _post(f"http://127.0.0.1:{port}/api/v1/agent/invoke", {"message": "hello"})
        assert status == 401
        payload = json.loads(body)
        assert payload["error"] == "invalid_api_key"
    finally:
        server.shutdown()
        server.server_close()


def test_gateway_invoke_returns_response_with_key(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)

    async def _fake_invoke(message: str, session_id: str) -> str:
        _ = session_id
        return f"echo:{message}"

    monkeypatch.setattr("zen_claw.dashboard.server._invoke_agent_text", _fake_invoke)
    raw, _ = generate_api_key()
    store_api_key(raw)
    port = _free_port()
    server = _start_gateway_health_server(port=port)
    try:
        status, body = _post(
            f"http://127.0.0.1:{port}/api/v1/agent/invoke",
            {"message": "invoke-ok"},
            api_key=raw,
        )
        assert status == 200
        payload = json.loads(body)
        assert payload["response"] == "echo:invoke-ok"
        assert payload["session_id"]
    finally:
        server.shutdown()
        server.server_close()


def test_gateway_agents_require_api_key(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)
    port = _free_port()
    server = _start_gateway_health_server(port=port)
    try:
        status, body = _request(f"http://127.0.0.1:{port}/api/v1/agents")
        assert status == 401
        payload = json.loads(body)
        assert payload["error"] == "invalid_api_key"
    finally:
        server.shutdown()
        server.server_close()


def test_gateway_agents_endpoints_return_dashboard_shapes(monkeypatch, tmp_path) -> None:
    from zen_claw.config.schema import Config

    cfg = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "workspace": str(tmp_path / "default-ws"),
                    "model": "default-model",
                    "enable_planning": True,
                },
                "profiles": {
                    "finance_writer": {
                        "display_name": "Finance Writer",
                        "description": "Handles finance copy",
                        "workspace": str(tmp_path / "finance-ws"),
                        "model": "deepseek-chat",
                        "system_prompt": "You are the finance agent.",
                        "skill_names": ["content_gen"],
                        "allowed_tools": ["rag_retrieve", "content_gen"],
                        "denied_tools": ["exec"],
                        "allowed_models": ["deepseek-chat", "gpt-4.1-mini"],
                        "cost_model": "gpt-4.1-nano",
                        "stability_model": "gpt-4.1",
                        "intent_model_overrides": {"finance": "gpt-4.1-mini"},
                        "task_type_model_overrides": {"message.send": "gpt-4.1-nano"},
                        "routing_keywords": ["finance", "bank campaign"],
                    }
                },
            },
            "channels": {
                "webchat": {
                    "enabled": True,
                    "agent_profile": "finance_writer",
                }
            },
        }
    )
    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda: cfg)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)
    raw, _ = generate_api_key()
    store_api_key(raw)
    port = _free_port()
    server = _start_gateway_health_server(port=port)
    try:
        status, body = _request(
            f"http://127.0.0.1:{port}/api/v1/agents",
            headers={"X-API-Key": raw},
        )
        assert status == 200
        payload = json.loads(body)
        assert payload["total"] == 2
        assert payload["agents"][1]["agent_id"] == "finance_writer"
        assert payload["agents"][1]["model"] == "deepseek-chat"
        assert payload["agents"][1]["routing_keywords"] == ["finance", "bank campaign"]
        assert payload["actions_total"] == 0
        assert payload["pending_reload"] is False
        assert payload["pending_reload_count"] == 0

        detail_status, detail_body = _request(
            f"http://127.0.0.1:{port}/api/v1/agents/finance_writer",
            headers={"X-API-Key": raw},
        )
        assert detail_status == 200
        detail = json.loads(detail_body)
        assert detail["agent_id"] == "finance_writer"
        assert detail["display_name"] == "Finance Writer"
        assert detail["effective"]["workspace"] == str((tmp_path / "finance-ws").resolve())
        assert detail["effective"]["model"] == "deepseek-chat"
        assert detail["effective"]["system_prompt"] == "You are the finance agent."
        assert detail["effective"]["skill_names"] == ["content_gen"]
        assert detail["effective"]["allowed_tools"] == ["rag_retrieve", "content_gen"]
        assert detail["effective"]["denied_tools"] == ["exec"]
        assert detail["effective"]["allowed_models"] == ["deepseek-chat", "gpt-4.1-mini"]
        assert detail["effective"]["cost_model"] == "gpt-4.1-nano"
        assert detail["effective"]["stability_model"] == "gpt-4.1"
        assert detail["effective"]["intent_model_overrides"] == {"finance": "gpt-4.1-mini"}
        assert detail["effective"]["task_type_model_overrides"] == {
            "message.send": "gpt-4.1-nano"
        }
        assert detail["profile_overrides"]["routing_keywords"] == ["finance", "bank campaign"]
        assert detail["profile_overrides"]["skill_names"] == ["content_gen"]
        assert detail["channel_references"] == [{"channel": "webchat", "display_name": "WebChat"}]
    finally:
        server.shutdown()
        server.server_close()


def test_gateway_webhook_trigger_accepts_signed_request(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("zen_claw.channels.webhook_trigger.get_data_dir", lambda: tmp_path)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)
    bus = MessageBus()
    channel = WebhookTriggerChannel(WebhookTriggerConfig(enabled=True, secret="s1"), bus)
    channel.access_checker = lambda *_args, **_kwargs: True
    port = _free_port()
    server = _start_gateway_health_server(port=port, webhook_trigger_channel=channel)
    body = b'{"content":"trigger now"}'
    ts = int(time.time())
    nonce = "n-1"
    try:
        status, text = _post_raw(
            f"http://127.0.0.1:{port}/webhook/trigger/default",
            body,
            headers={
                "Content-Type": "application/json",
                "X-Signature": _sign("s1", body, ts, nonce),
                "X-Timestamp": str(ts),
                "X-Nonce": nonce,
            },
        )
        assert status == 202
        payload = json.loads(text)
        assert payload["success"] is True
        assert payload["agent_id"] == "default"
        inbound = asyncio.run(bus.consume_inbound())
        assert inbound.content == "trigger now"
    finally:
        server.shutdown()
        server.server_close()


def test_gateway_webhook_trigger_rejects_bad_signature(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("zen_claw.channels.webhook_trigger.get_data_dir", lambda: tmp_path)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)
    bus = MessageBus()
    channel = WebhookTriggerChannel(WebhookTriggerConfig(enabled=True, secret="s1"), bus)
    channel.access_checker = lambda *_args, **_kwargs: True
    port = _free_port()
    server = _start_gateway_health_server(port=port, webhook_trigger_channel=channel)
    body = b'{"content":"trigger now"}'
    ts = int(time.time())
    nonce = "n-2"
    try:
        status, text = _post_raw(
            f"http://127.0.0.1:{port}/webhook/trigger/default",
            body,
            headers={
                "Content-Type": "application/json",
                "X-Signature": _sign("bad-secret", body, ts, nonce),
                "X-Timestamp": str(ts),
                "X-Nonce": nonce,
            },
        )
        assert status == 403
        payload = json.loads(text)
        assert payload["success"] is False
        assert payload["reason"] == "invalid_signature"
    finally:
        server.shutdown()
        server.server_close()

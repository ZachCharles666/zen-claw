import asyncio
import hashlib
import hmac
import json
import time

import pytest

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover
    TestClient = None

from zen_claw.bus.queue import MessageBus
from zen_claw.channels.webhook_trigger import WebhookTriggerChannel
from zen_claw.config.schema import WebhookTriggerConfig
from zen_claw.dashboard.server import api_app
from zen_claw.dashboard.webhooks import register_channels


def _sign(secret: str, body: bytes, ts: int, nonce: str) -> str:
    payload = f"{ts}.{nonce}.".encode("utf-8") + body
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def test_webhook_trigger_validate_and_replay(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("zen_claw.channels.webhook_trigger.get_data_dir", lambda: tmp_path)
    bus = MessageBus()
    channel = WebhookTriggerChannel(
        WebhookTriggerConfig(
            enabled=True,
            secret="s1",
            timestamp_tolerance_sec=300,
            nonce_ttl_sec=600,
        ),
        bus,
    )
    body = b'{"content":"hello"}'
    ts = int(time.time())
    nonce = "n-1"
    headers = {
        "x-signature": _sign("s1", body, ts, nonce),
        "x-timestamp": str(ts),
        "x-nonce": nonce,
    }
    ok, reason = channel.validate_request(body=body, headers=headers, client_ip="8.8.8.8")
    assert ok is True and reason == ""
    ok2, reason2 = channel.validate_request(body=body, headers=headers, client_ip="8.8.8.8")
    assert ok2 is False
    assert reason2 == "replayed_nonce"


def test_webhook_trigger_ingest_publishes_inbound(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("zen_claw.channels.webhook_trigger.get_data_dir", lambda: tmp_path)
    bus = MessageBus()
    channel = WebhookTriggerChannel(
        WebhookTriggerConfig(enabled=True, ip_allowlist=["127.0.0.1"]), bus
    )
    channel.access_checker = lambda *_args, **_kwargs: True

    async def _run():
        await channel.ingest_trigger(
            agent_id="agent-a",
            payload={"content": "run task", "chat_id": "chat-a"},
            client_ip="127.0.0.1",
        )
        inbound = await bus.consume_inbound()
        assert inbound.channel == "webhook_trigger"
        assert inbound.chat_id == "chat-a"
        assert inbound.content == "run task"

    asyncio.run(_run())


@pytest.mark.skipif(TestClient is None or api_app is None, reason="fastapi not available")
def test_webhook_trigger_route_accepts_signed_request(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("zen_claw.channels.webhook_trigger.get_data_dir", lambda: tmp_path)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)
    bus = MessageBus()
    channel = WebhookTriggerChannel(WebhookTriggerConfig(enabled=True, secret="s1"), bus)
    channel.access_checker = lambda *_args, **_kwargs: True
    register_channels(webhook_trigger=channel)
    client = TestClient(api_app)
    body = b'{"content":"trigger now"}'
    ts = int(time.time())
    nonce = "n-2"
    resp = client.post(
        "/webhook/trigger/agent-z",
        content=body,
        headers={
            "content-type": "application/json",
            "x-signature": _sign("s1", body, ts, nonce),
            "x-timestamp": str(ts),
            "x-nonce": nonce,
        },
    )
    assert resp.status_code == 202
    inbound = asyncio.run(bus.consume_inbound())
    assert inbound.content == "trigger now"


def test_webhook_trigger_ingest_extracts_workflow_context(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("zen_claw.channels.webhook_trigger.get_data_dir", lambda: tmp_path)
    bus = MessageBus()
    channel = WebhookTriggerChannel(
        WebhookTriggerConfig(enabled=True, ip_allowlist=["127.0.0.1"]), bus
    )
    channel.access_checker = lambda *_args, **_kwargs: True

    async def _run():
        accepted = await channel.ingest_trigger(
            agent_id="agent-flow",
            payload={"content": "run workflow", "workflow_source": "n8n", "run_id": "run-9"},
            headers={"x-workflow-step": "fetch_weather", "x-request-id": "req-7"},
            query={"step": "ignored-query-step"},
            client_ip="127.0.0.1",
        )
        inbound = await bus.consume_inbound()
        assert accepted["trace_id"]
        assert accepted["workflow_source"] == "n8n"
        assert accepted["workflow_run_id"] == "run-9"
        assert accepted["workflow_step"] == "fetch_weather"
        assert inbound.metadata["workflow_source"] == "n8n"
        assert inbound.metadata["workflow_run_id"] == "run-9"
        assert inbound.metadata["workflow_step"] == "fetch_weather"
        assert inbound.metadata["external_request_id"] == "req-7"
        assert inbound.metadata["trace_id"] == accepted["trace_id"]

    asyncio.run(_run())


@pytest.mark.skipif(TestClient is None or api_app is None, reason="fastapi not available")
def test_webhook_trigger_route_returns_trace_and_workflow_context(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("zen_claw.channels.webhook_trigger.get_data_dir", lambda: tmp_path)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)
    bus = MessageBus()
    channel = WebhookTriggerChannel(WebhookTriggerConfig(enabled=True, secret="s1"), bus)
    channel.access_checker = lambda *_args, **_kwargs: True
    register_channels(webhook_trigger=channel)
    client = TestClient(api_app)
    body = b'{"content":"trigger workflow","workflow_source":"coze","run_id":"job-1"}'
    ts = int(time.time())
    nonce = "n-3"
    resp = client.post(
        "/webhook/trigger/agent-flow",
        content=body,
        headers={
            "content-type": "application/json",
            "x-signature": _sign("s1", body, ts, nonce),
            "x-timestamp": str(ts),
            "x-nonce": nonce,
            "x-workflow-step": "node-a",
        },
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["success"] is True
    assert data["trace_id"]
    assert data["workflow_source"] == "coze"
    assert data["workflow_run_id"] == "job-1"
    assert data["workflow_step"] == "node-a"
    inbound = asyncio.run(bus.consume_inbound())
    assert inbound.metadata["trace_id"] == data["trace_id"]
    log_path = tmp_path / "dashboard" / "workflow_webhook.log.jsonl"
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    assert rows[-1]["trace_id"] == data["trace_id"]
    assert rows[-1]["workflow_source"] == "coze"

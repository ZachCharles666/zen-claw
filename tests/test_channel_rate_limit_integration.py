import asyncio
import json
from pathlib import Path

import pytest

from zen_claw.bus.events import OutboundMessage
from zen_claw.bus.queue import MessageBus
from zen_claw.channels.manager import ChannelManager
from zen_claw.config.schema import ChannelRateLimitConfig, Config


class _CollectChannel:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self._running = True

    async def send(self, msg: OutboundMessage) -> None:
        self.sent.append(str(msg.content))

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running


@pytest.mark.asyncio
async def test_channel_manager_dispatch_drop_mode_records_runtime_stats(
    tmp_path: Path, monkeypatch
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)

    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path / "ws")
    cfg.channels.outbound_rate_limit_mode = "drop"
    cfg.channels.outbound_rate_limit_per_sec = 0.2
    cfg.channels.outbound_rate_limit_burst = 1
    cfg.channels.outbound_rate_limit_drop_notice = False
    cfg.channels.outbound_rate_limit_by_channel = {
        "webchat": ChannelRateLimitConfig(per_sec=0.2, burst=1, mode="drop")
    }
    cfg.tools.policy.trusted_local_channels = ["cli", "system", "webchat"]

    bus = MessageBus()
    mgr = ChannelManager(cfg, bus)
    ch = _CollectChannel()
    mgr.channels["webchat"] = ch  # Inject fake channel for dispatcher integration.

    task = asyncio.create_task(mgr._dispatch_outbound())
    try:
        await bus.publish_outbound(OutboundMessage(channel="webchat", chat_id="u1", content="m1"))
        await bus.publish_outbound(OutboundMessage(channel="webchat", chat_id="u1", content="m2"))
        await asyncio.sleep(0.35)
    finally:
        task.cancel()
        await task

    # First message sent, second dropped by limiter (burst=1, no refill in test window).
    assert ch.sent == ["m1"]

    stats_file = data_dir / "channels" / "rate_limit_stats.json"
    assert stats_file.exists()
    stats = json.loads(stats_file.read_text(encoding="utf-8"))
    row = stats["channels"]["webchat"]
    assert int(row["dropped_count"]) >= 1


@pytest.mark.asyncio
async def test_channel_manager_external_outbound_fails_closed_without_sidecar(
    tmp_path: Path, monkeypatch
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)

    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path / "ws")

    bus = MessageBus()
    mgr = ChannelManager(cfg, bus)
    ch = _CollectChannel()
    mgr.channels["discord"] = ch

    task = asyncio.create_task(mgr._dispatch_outbound())
    try:
        await bus.publish_outbound(OutboundMessage(channel="discord", chat_id="u1", content="m1"))
        await asyncio.sleep(0.2)
    finally:
        task.cancel()
        await task

    assert ch.sent == []


@pytest.mark.asyncio
async def test_channel_manager_external_outbound_uses_sidecar_when_configured(
    tmp_path: Path, monkeypatch
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)

    cfg = Config.model_validate(
        {
            "agents": {"defaults": {"workspace": str(tmp_path / "ws")}},
            "tools": {
                "policy": {
                    "connector_allowed_names": ["discord"],
                    "connector_allowed_actions": ["connector.message.send"],
                    "connector_allowed_target_resources": ["channel.discord.message"],
                }
            },
        }
    )

    calls: list[tuple[str, dict]] = []

    async def _fake_execute_request(self, *, request_payload, trace_id):  # noqa: ANN001
        calls.append((trace_id, request_payload))
        from zen_claw.agent.tools.result import ToolResult

        return ToolResult.success("ok", sidecar_target=self.proxy_url)

    monkeypatch.setattr(
        "zen_claw.agent.tools.connector_sidecar.ConnectorSidecarClient.execute_request",
        _fake_execute_request,
    )

    bus = MessageBus()
    mgr = ChannelManager(cfg, bus)
    ch = _CollectChannel()
    mgr.channels["discord"] = ch

    task = asyncio.create_task(mgr._dispatch_outbound())
    try:
        await bus.publish_outbound(OutboundMessage(channel="discord", chat_id="u1", content="m1"))
        await asyncio.sleep(0.2)
    finally:
        task.cancel()
        await task

    assert ch.sent == []
    assert len(calls) == 1
    trace_id, payload = calls[0]
    assert trace_id
    assert payload["connector_meta"]["connector_name"] == "discord"
    assert payload["connector_meta"]["action"] == "connector.message.send"

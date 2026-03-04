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
async def test_channel_manager_dispatch_drop_mode_records_runtime_stats(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)

    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path / "ws")
    cfg.channels.outbound_rate_limit_mode = "drop"
    cfg.channels.outbound_rate_limit_per_sec = 0.2
    cfg.channels.outbound_rate_limit_burst = 1
    cfg.channels.outbound_rate_limit_drop_notice = False
    cfg.channels.outbound_rate_limit_by_channel = {
        "discord": ChannelRateLimitConfig(per_sec=0.2, burst=1, mode="drop")
    }

    bus = MessageBus()
    mgr = ChannelManager(cfg, bus)
    ch = _CollectChannel()
    mgr.channels["discord"] = ch  # Inject fake channel for dispatcher integration.

    task = asyncio.create_task(mgr._dispatch_outbound())
    try:
        await bus.publish_outbound(OutboundMessage(channel="discord", chat_id="u1", content="m1"))
        await bus.publish_outbound(OutboundMessage(channel="discord", chat_id="u1", content="m2"))
        await asyncio.sleep(0.35)
    finally:
        task.cancel()
        await task

    # First message sent, second dropped by limiter (burst=1, no refill in test window).
    assert ch.sent == ["m1"]

    stats_file = data_dir / "channels" / "rate_limit_stats.json"
    assert stats_file.exists()
    stats = json.loads(stats_file.read_text(encoding="utf-8"))
    row = stats["channels"]["discord"]
    assert int(row["dropped_count"]) >= 1

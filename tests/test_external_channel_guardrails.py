from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from zen_claw.bus.events import OutboundMessage
from zen_claw.bus.queue import MessageBus
from zen_claw.channels.slack import SlackChannel
from zen_claw.channels.telegram import TelegramChannel
from zen_claw.config.schema import SlackConfig, TelegramConfig


@pytest.mark.asyncio
async def test_external_channel_direct_send_blocked_even_with_legacy_compat(monkeypatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setattr("zen_claw.channels.base.get_data_dir", lambda: data_dir)

    sent: list[dict[str, object]] = []

    class _Bot:
        async def send_message(self, **kwargs):
            sent.append(kwargs)

    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="tg-token"),
        MessageBus(),
        media_root=tmp_path / "media",
    )
    channel._app = SimpleNamespace(bot=_Bot())
    channel.security_policy = SimpleNamespace(
        production_hardening=True,
        legacy_compat=True,
        trusted_local_channels=["cli", "system"],
    )

    with pytest.raises(RuntimeError, match="authorized dispatch context"):
        await channel.send(OutboundMessage(channel="telegram", chat_id="12345", content="hello"))

    assert sent == []
    audit_log = data_dir / "audit_logs"
    rows = []
    for path in audit_log.glob("*.jsonl"):
        rows.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line)
    assert any(str(row.get("event_type") or "") == "message.outbound.direct_blocked" for row in rows)


@pytest.mark.asyncio
async def test_external_channel_helper_blocked_without_authorized_context(
    monkeypatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setattr("zen_claw.channels.base.get_data_dir", lambda: data_dir)

    class _Client:
        async def files_upload_v2(self, **kwargs):
            return kwargs

    media_file = tmp_path / "file.txt"
    media_file.write_text("hello", encoding="utf-8")

    channel = SlackChannel(
        SlackConfig(enabled=True, bot_token="xoxb-test"),
        MessageBus(),
        media_root=tmp_path / "media",
    )
    channel._client = _Client()
    channel.security_policy = SimpleNamespace(
        production_hardening=True,
        legacy_compat=False,
        trusted_local_channels=["cli", "system"],
    )

    with pytest.raises(RuntimeError, match="authorized dispatch context"):
        await channel._upload_files(
            OutboundMessage(channel="slack", chat_id="C1", content="hello", media=[str(media_file)])
        )

    audit_log = data_dir / "audit_logs"
    rows = []
    for path in audit_log.glob("*.jsonl"):
        rows.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line)
    assert any(str(row.get("event_type") or "") == "message.outbound.helper_blocked" for row in rows)

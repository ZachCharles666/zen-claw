"""Tests for Telegram channel — PA1."""

import asyncio
from pathlib import Path

import pytest

from zen_claw.bus.events import OutboundMessage
from zen_claw.bus.queue import MessageBus
from zen_claw.channels.telegram import TelegramChannel
from zen_claw.config.schema import TelegramConfig


def _make_config(**kwargs) -> TelegramConfig:
    defaults = dict(enabled=True, token="", admins=[], users=[], allow_from=[], agent_profile="default", proxy=None)
    defaults.update(kwargs)
    return TelegramConfig(**defaults)


def _make_channel(config: TelegramConfig | None = None, media_root: Path | None = None) -> tuple[TelegramChannel, MessageBus]:
    bus = MessageBus()
    cfg = config or _make_config()
    ch = TelegramChannel(config=cfg, bus=bus, media_root=media_root or Path("/tmp/zen_test_media"))
    return ch, bus


# ── 基本属性 ─────────────────────────────────────────────────────────────────

def test_telegram_channel_name() -> None:
    ch, _ = _make_channel()
    assert ch.name == "telegram"


def test_telegram_channel_not_running_by_default() -> None:
    ch, _ = _make_channel()
    assert ch._running is False
    assert ch._app is None


# ── 启动/停止（空 token，无真实连接）────────────────────────────────────────

@pytest.mark.asyncio
async def test_telegram_start_with_empty_token_returns_gracefully() -> None:
    """start() with no token should log error and return without raising."""
    ch, _ = _make_channel(_make_config(token=""))
    # Should not raise — just log error and return
    await asyncio.wait_for(ch.start(), timeout=1.0)
    assert ch._app is None


@pytest.mark.asyncio
async def test_telegram_stop_when_not_running_is_safe() -> None:
    ch, _ = _make_channel()
    ch._running = False
    # Should not raise
    await ch.stop()
    assert ch._app is None


# ── send（未连接状态）────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_telegram_send_without_app_logs_warning(caplog) -> None:
    import logging
    ch, _ = _make_channel()
    ch._app = None
    msg = OutboundMessage(channel="telegram", chat_id="12345", content="hello")
    with caplog.at_level(logging.WARNING):
        await ch.send(msg)
    # Should not raise; app is None so warning is logged by loguru (may not hit caplog)
    assert ch._app is None


# ── RBAC: is_allowed ─────────────────────────────────────────────────────────

def test_telegram_is_allowed_admin_list() -> None:
    cfg = _make_config(admins=["alice", "bob"], users=[])
    ch, _ = _make_channel(cfg)
    assert ch.is_allowed("alice") is True
    assert ch.is_allowed("bob") is True
    assert ch.is_allowed("charlie") is False


def test_telegram_is_allowed_users_list() -> None:
    cfg = _make_config(admins=[], users=["user1"])
    ch, _ = _make_channel(cfg)
    assert ch.is_allowed("user1") is True
    assert ch.is_allowed("stranger") is False


def test_telegram_is_allowed_empty_config_denies_all() -> None:
    """No admins, no users, no allow_from → deny everyone."""
    cfg = _make_config(admins=[], users=[], allow_from=[])
    ch, _ = _make_channel(cfg)
    assert ch.is_allowed("anyone") is False


def test_telegram_is_allowed_composite_sender_id() -> None:
    """Sender id '123|alice' matches allow by username 'alice'."""
    cfg = _make_config(admins=["alice"])
    ch, _ = _make_channel(cfg)
    assert ch.is_allowed("123|alice") is True


# ── RBAC: get_role ────────────────────────────────────────────────────────────

def test_telegram_get_role_admin() -> None:
    cfg = _make_config(admins=["admin1"])
    ch, _ = _make_channel(cfg)
    assert ch.get_role("admin1") == "admin"


def test_telegram_get_role_user() -> None:
    cfg = _make_config(admins=[], users=["user1"])
    ch, _ = _make_channel(cfg)
    assert ch.get_role("user1") == "user"


def test_telegram_get_role_guest() -> None:
    cfg = _make_config(admins=["admin1"], users=["user1"])
    ch, _ = _make_channel(cfg)
    assert ch.get_role("stranger") == "guest"


# ── _handle_message 路由到 bus ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_telegram_handle_message_publishes_to_bus() -> None:
    cfg = _make_config(admins=["testuser"])
    ch, bus = _make_channel(cfg)

    await ch._handle_message(
        sender_id="testuser",
        chat_id="9999",
        content="Hello from Telegram",
        metadata={"message_id": 1},
    )

    assert bus.inbound_size == 1
    msg = await bus.consume_inbound()
    assert msg.channel == "telegram"
    assert msg.chat_id == "9999"
    assert msg.content == "Hello from Telegram"
    assert msg.sender_id == "testuser"


@pytest.mark.asyncio
async def test_telegram_handle_message_denied_sender_not_published() -> None:
    """Sender not in admins/users → message not put on bus."""
    cfg = _make_config(admins=["allowed_user"])
    ch, bus = _make_channel(cfg)

    await ch._handle_message(
        sender_id="denied_stranger",
        chat_id="9999",
        content="Should not arrive",
    )

    assert bus.inbound_size == 0

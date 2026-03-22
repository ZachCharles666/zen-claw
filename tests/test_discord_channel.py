"""Tests for Discord channel — PA2."""

import asyncio
from pathlib import Path

import pytest

from zen_claw.bus.events import OutboundMessage
from zen_claw.bus.queue import MessageBus
from zen_claw.channels.discord import DiscordChannel
from zen_claw.config.schema import DiscordConfig


def _make_config(**kwargs) -> DiscordConfig:
    defaults = dict(
        enabled=True,
        token="",
        admins=[],
        users=[],
        allow_from=[],
        gateway_url="wss://gateway.discord.gg/?v=10&encoding=json",
        intents=37377,
        agent_profile="default",
    )
    defaults.update(kwargs)
    return DiscordConfig(**defaults)


def _make_channel(config: DiscordConfig | None = None, media_root: Path | None = None) -> tuple[DiscordChannel, MessageBus]:
    bus = MessageBus()
    cfg = config or _make_config()
    ch = DiscordChannel(config=cfg, bus=bus, media_root=media_root or Path("/tmp/zen_test_media"))
    return ch, bus


# ── 基本属性 ─────────────────────────────────────────────────────────────────

def test_discord_channel_name() -> None:
    ch, _ = _make_channel()
    assert ch.name == "discord"


def test_discord_channel_not_running_by_default() -> None:
    ch, _ = _make_channel()
    assert ch._running is False
    assert ch._ws is None
    assert ch._http is None


# ── 启动/停止（空 token，无真实连接）────────────────────────────────────────

@pytest.mark.asyncio
async def test_discord_start_with_empty_token_returns_gracefully() -> None:
    """start() with no token should log error and return without raising."""
    ch, _ = _make_channel(_make_config(token=""))
    await asyncio.wait_for(ch.start(), timeout=1.0)
    assert ch._ws is None


@pytest.mark.asyncio
async def test_discord_stop_when_not_running_is_safe() -> None:
    ch, _ = _make_channel()
    ch._running = False
    # Should not raise even if http/ws not initialized
    await ch.stop()
    assert ch._ws is None
    assert ch._http is None


# ── send（未连接状态）────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_discord_send_without_http_logs_warning() -> None:
    ch, _ = _make_channel()
    ch._http = None
    msg = OutboundMessage(channel="discord", chat_id="123456789", content="hello")
    # Should not raise
    await ch.send(msg)
    assert ch._http is None


# ── RBAC: is_allowed ─────────────────────────────────────────────────────────

def test_discord_is_allowed_admin_list() -> None:
    cfg = _make_config(admins=["admin_user"], users=[])
    ch, _ = _make_channel(cfg)
    assert ch.is_allowed("admin_user") is True
    assert ch.is_allowed("random_user") is False


def test_discord_is_allowed_users_list() -> None:
    cfg = _make_config(admins=[], users=["member1", "member2"])
    ch, _ = _make_channel(cfg)
    assert ch.is_allowed("member1") is True
    assert ch.is_allowed("outsider") is False


def test_discord_is_allowed_empty_config_denies_all() -> None:
    """No admins, no users, no allow_from → deny everyone."""
    cfg = _make_config(admins=[], users=[], allow_from=[])
    ch, _ = _make_channel(cfg)
    assert ch.is_allowed("anyone") is False


def test_discord_is_allowed_allow_from_list() -> None:
    cfg = _make_config(admins=[], users=[], allow_from=["trusted_user"])
    ch, _ = _make_channel(cfg)
    assert ch.is_allowed("trusted_user") is True
    assert ch.is_allowed("untrusted") is False


# ── RBAC: get_role ────────────────────────────────────────────────────────────

def test_discord_get_role_admin() -> None:
    cfg = _make_config(admins=["server_admin"])
    ch, _ = _make_channel(cfg)
    assert ch.get_role("server_admin") == "admin"


def test_discord_get_role_user() -> None:
    cfg = _make_config(admins=[], users=["regular_user"])
    ch, _ = _make_channel(cfg)
    assert ch.get_role("regular_user") == "user"


def test_discord_get_role_guest() -> None:
    cfg = _make_config(admins=["admin1"], users=["user1"])
    ch, _ = _make_channel(cfg)
    assert ch.get_role("unknown") == "guest"


# ── _handle_message 路由到 bus ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_discord_handle_message_publishes_to_bus() -> None:
    cfg = _make_config(admins=["discord_user"])
    ch, bus = _make_channel(cfg)

    await ch._handle_message(
        sender_id="discord_user",
        chat_id="channel_abc",
        content="Hello from Discord",
        metadata={"message_id": "msg_123", "guild_id": "guild_456"},
    )

    assert bus.inbound_size == 1
    msg = await bus.consume_inbound()
    assert msg.channel == "discord"
    assert msg.chat_id == "channel_abc"
    assert msg.content == "Hello from Discord"
    assert msg.sender_id == "discord_user"


@pytest.mark.asyncio
async def test_discord_handle_message_denied_sender_not_published() -> None:
    cfg = _make_config(admins=["allowed_user"])
    ch, bus = _make_channel(cfg)

    await ch._handle_message(
        sender_id="intruder",
        chat_id="channel_abc",
        content="Should not arrive",
    )

    assert bus.inbound_size == 0


@pytest.mark.asyncio
async def test_discord_handle_message_attaches_channel_role() -> None:
    """Messages from admins should have channel_role=admin in metadata."""
    cfg = _make_config(admins=["admin_user"])
    ch, bus = _make_channel(cfg)

    await ch._handle_message(
        sender_id="admin_user",
        chat_id="chan_1",
        content="Admin message",
    )

    msg = await bus.consume_inbound()
    assert msg.metadata.get("channel_role") == "admin"

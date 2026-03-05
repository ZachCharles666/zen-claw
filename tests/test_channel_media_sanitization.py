import asyncio
from pathlib import Path
from types import SimpleNamespace

from zen_claw.bus.queue import MessageBus
from zen_claw.channels.base import BaseChannel


class DummyChannel(BaseChannel):
    name = "dummy"

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg) -> None:
        return None


def test_sanitize_media_filters_invalid_and_dedupes() -> None:
    ch = DummyChannel(SimpleNamespace(allow_from=[]), MessageBus())
    media = [" a.png ", "", "a.png", None, "b.mp3"]  # type: ignore[list-item]
    out = ch._sanitize_media(media)
    assert out == ["a.png", "b.mp3"]


def test_sanitize_media_limits_items() -> None:
    ch = DummyChannel(SimpleNamespace(allow_from=[]), MessageBus())
    media = [f"f{i}.png" for i in range(30)]
    out = ch._sanitize_media(media)
    assert len(out) == ch._MAX_MEDIA_ITEMS


def test_handle_message_uses_sanitized_media() -> None:
    bus = MessageBus()
    # u1 must be explicitly allowed; empty config now denies all (MEDIUM-003 fix)
    ch = DummyChannel(SimpleNamespace(allow_from=[], admins=["u1"], users=[]), bus)
    asyncio.run(
        ch._handle_message(
            sender_id="u1",
            chat_id="c1",
            content="hello",
            media=["x.png", "x.png", ""],
            metadata={"k": "v"},
        )
    )
    msg = asyncio.run(bus.consume_inbound())
    assert msg.media == ["x.png"]
    assert msg.metadata.get("channel_role") == "admin"


def test_handle_message_merges_metadata_media_refs() -> None:
    bus = MessageBus()
    # u1 must be explicitly allowed; empty config now denies all (MEDIUM-003 fix)
    ch = DummyChannel(SimpleNamespace(allow_from=[], admins=["u1"], users=[]), bus)
    asyncio.run(
        ch._handle_message(
            sender_id="u1",
            chat_id="c1",
            content="hello",
            media=["x.png"],
            metadata={"media_refs": ["media://telegram/image/a1", "media://telegram/image/a1"]},
        )
    )
    msg = asyncio.run(bus.consume_inbound())
    assert msg.media == ["x.png", "media://telegram/image/a1"]


def test_channel_media_root_default_and_override(tmp_path: Path) -> None:
    default_ch = DummyChannel(SimpleNamespace(allow_from=[]), MessageBus())
    assert ".zen-claw" in str(default_ch.media_root)

    custom_root = tmp_path / "media"
    custom_ch = DummyChannel(SimpleNamespace(allow_from=[]), MessageBus(), media_root=custom_root)
    assert custom_ch.media_root == custom_root


def test_channel_rbac_uses_admins_and_users_over_allow_from() -> None:
    config = SimpleNamespace(
        allow_from=["legacy-allowed"],
        admins=["admin-1"],
        users=["user-1"],
    )
    ch = DummyChannel(config, MessageBus())
    assert ch.is_allowed("admin-1") is True
    assert ch.is_allowed("user-1") is True
    assert ch.is_allowed("legacy-allowed") is False
    assert ch.get_role("admin-1") == "admin"
    assert ch.get_role("user-1") == "user"


def test_channel_rbac_matches_compound_sender_tokens() -> None:
    config = SimpleNamespace(
        allow_from=[],
        admins=["alice"],
        users=[],
    )
    ch = DummyChannel(config, MessageBus())
    assert ch.is_allowed("123|alice") is True
    assert ch.get_role("123|alice") == "admin"

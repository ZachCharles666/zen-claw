import asyncio
from pathlib import Path

from zen_claw.bus.queue import MessageBus
from zen_claw.channels.discord import DiscordChannel
from zen_claw.config.schema import DiscordConfig


class _FakeResponse:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self) -> None:
        return None


class _FakeHTTP:
    async def get(self, url: str):
        return _FakeResponse(b"media-bytes")

    async def post(self, url: str, headers=None, json=None):
        return _FakeResponse(b"{}")


def test_discord_handle_message_create_downloads_and_transcribes(tmp_path: Path) -> None:
    ch = DiscordChannel(
        DiscordConfig(admins=["u1"]),  # explicit allow (default-deny after MEDIUM-003)
        MessageBus(),
        media_root=tmp_path / "media",
        groq_api_key="k",
    )
    ch._http = _FakeHTTP()

    captured = {}

    async def _capture_handle_message(sender_id, chat_id, content, media=None, metadata=None):
        captured["sender_id"] = sender_id
        captured["chat_id"] = chat_id
        captured["content"] = content
        captured["media"] = media or []
        captured["metadata"] = metadata or {}

    async def _noop_typing(channel_id: str):
        return None

    async def _fake_transcribe(file_path, content_type):
        return "voice text"

    ch._handle_message = _capture_handle_message  # type: ignore[method-assign]
    ch._start_typing = _noop_typing  # type: ignore[method-assign]
    ch._maybe_transcribe_media = _fake_transcribe  # type: ignore[method-assign]

    payload = {
        "author": {"id": "u1", "bot": False},
        "channel_id": "c1",
        "content": "hello",
        "id": "m1",
        "attachments": [
            {
                "id": "a1",
                "url": "https://example.test/a1.mp3",
                "filename": "a1.mp3",
                "size": 128,
                "content_type": "audio/mpeg",
            }
        ],
    }

    asyncio.run(ch._handle_message_create(payload))

    assert captured["sender_id"] == "u1"
    assert captured["chat_id"] == "c1"
    assert "hello" in captured["content"]
    assert "[transcription: voice text]" in captured["content"]
    assert "media://discord/audio/a1" in captured["content"]
    assert len(captured["media"]) == 1
    saved = Path(captured["media"][0])
    assert saved.exists()
    assert str(saved).startswith(str((tmp_path / "media").resolve()))
    assert captured["metadata"]["media_refs"] == ["media://discord/audio/a1"]


def test_discord_handle_message_create_rejects_oversized_attachment(tmp_path: Path) -> None:
    ch = DiscordChannel(
        DiscordConfig(admins=["u1"]),  # explicit allow (default-deny after MEDIUM-003)
        MessageBus(),
        media_root=tmp_path / "media",
        groq_api_key="",
    )
    ch._http = _FakeHTTP()

    captured = {}

    async def _capture_handle_message(sender_id, chat_id, content, media=None, metadata=None):
        captured["content"] = content
        captured["media"] = media or []

    async def _noop_typing(channel_id: str):
        return None

    ch._handle_message = _capture_handle_message  # type: ignore[method-assign]
    ch._start_typing = _noop_typing  # type: ignore[method-assign]

    payload = {
        "author": {"id": "u1", "bot": False},
        "channel_id": "c1",
        "content": "",
        "id": "m1",
        "attachments": [
            {
                "id": "a1",
                "url": "https://example.test/large.bin",
                "filename": "large.bin",
                "size": 25 * 1024 * 1024,
                "content_type": "application/octet-stream",
            }
        ],
    }

    asyncio.run(ch._handle_message_create(payload))
    assert "too large" in captured["content"]
    assert captured["media"] == []


def test_discord_handle_message_create_caps_attachments_processed(tmp_path: Path) -> None:
    ch = DiscordChannel(
        DiscordConfig(admins=["u1"]),  # explicit allow (default-deny after MEDIUM-003)
        MessageBus(),
        media_root=tmp_path / "media",
        groq_api_key="",
    )
    ch._http = _FakeHTTP()

    captured = {}

    async def _capture_handle_message(sender_id, chat_id, content, media=None, metadata=None):
        captured["media"] = media or []

    async def _noop_typing(channel_id: str):
        return None

    ch._handle_message = _capture_handle_message  # type: ignore[method-assign]
    ch._start_typing = _noop_typing  # type: ignore[method-assign]

    attachments = []
    for i in range(12):
        attachments.append(
            {
                "id": f"a{i}",
                "url": f"https://example.test/{i}.png",
                "filename": f"{i}.png",
                "size": 16,
                "content_type": "image/png",
            }
        )
    payload = {
        "author": {"id": "u1", "bot": False},
        "channel_id": "c1",
        "content": "",
        "id": "m1",
        "attachments": attachments,
    }

    asyncio.run(ch._handle_message_create(payload))
    assert len(captured["media"]) == 8



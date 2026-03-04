import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from zen_claw.bus.queue import MessageBus
from zen_claw.config.schema import TelegramConfig

pytest.importorskip("telegram")
from zen_claw.channels.telegram import TelegramChannel


class _FakeTelegramFile:
    def __init__(self, data: bytes):
        self._data = data

    async def download_to_drive(self, path: str) -> None:
        Path(path).write_bytes(self._data)


class _FakeTelegramBot:
    def __init__(self, data: bytes):
        self._data = data

    async def get_file(self, file_id: str):
        return _FakeTelegramFile(self._data)


def _mk_update(message, user) -> SimpleNamespace:
    return SimpleNamespace(message=message, effective_user=user)


def test_telegram_on_message_downloads_photo_to_media_root(tmp_path: Path) -> None:
    ch = TelegramChannel(
        TelegramConfig(),
        MessageBus(),
        groq_api_key="",
        media_root=tmp_path / "media",
    )
    ch._app = SimpleNamespace(bot=_FakeTelegramBot(b"img-bytes"))

    captured = {}

    async def _capture_handle_message(sender_id, chat_id, content, media=None, metadata=None):
        captured["sender_id"] = sender_id
        captured["chat_id"] = chat_id
        captured["content"] = content
        captured["media"] = media or []
        captured["metadata"] = metadata or {}

    ch._handle_message = _capture_handle_message  # type: ignore[method-assign]

    user = SimpleNamespace(id=123, username="alice", first_name="Alice")
    photo_obj = SimpleNamespace(file_id="photo_file_id_1234567890")
    chat = SimpleNamespace(type="private")
    msg = SimpleNamespace(
        chat_id=999,
        text="hello",
        caption=None,
        photo=[photo_obj],
        voice=None,
        audio=None,
        document=None,
        message_id=77,
        chat=chat,
    )
    update = _mk_update(msg, user)

    asyncio.run(ch._on_message(update, context=None))

    assert captured["sender_id"] == "123|alice"
    assert captured["chat_id"] == "999"
    assert "hello" in captured["content"]
    assert "media://telegram/image/photo_file_id_1234567890" in captured["content"]
    assert "[image:" in captured["content"]
    assert len(captured["media"]) == 1
    assert captured["metadata"]["media_refs"] == ["media://telegram/image/photo_file_id_1234567890"]
    saved = Path(captured["media"][0])
    assert saved.exists()
    assert str(saved).startswith(str((tmp_path / "media").resolve()))


def test_telegram_on_message_transcribes_voice_when_key_present(monkeypatch, tmp_path: Path) -> None:
    class _FakeTranscriber:
        def __init__(self, api_key: str = ""):
            self.api_key = api_key

        async def transcribe(self, file_path):
            return "voice text"

    monkeypatch.setattr("zen_claw.providers.transcription.GroqTranscriptionProvider", _FakeTranscriber)

    ch = TelegramChannel(
        TelegramConfig(),
        MessageBus(),
        groq_api_key="groq-key",
        media_root=tmp_path / "media",
    )
    ch._app = SimpleNamespace(bot=_FakeTelegramBot(b"audio-bytes"))

    captured = {}

    async def _capture_handle_message(sender_id, chat_id, content, media=None, metadata=None):
        captured["content"] = content
        captured["media"] = media or []
        captured["metadata"] = metadata or {}

    ch._handle_message = _capture_handle_message  # type: ignore[method-assign]

    user = SimpleNamespace(id=123, username=None, first_name="Alice")
    voice_obj = SimpleNamespace(file_id="voice_file_id_1234567890", mime_type="audio/ogg")
    chat = SimpleNamespace(type="private")
    msg = SimpleNamespace(
        chat_id=999,
        text=None,
        caption=None,
        photo=None,
        voice=voice_obj,
        audio=None,
        document=None,
        message_id=88,
        chat=chat,
    )
    update = _mk_update(msg, user)

    asyncio.run(ch._on_message(update, context=None))

    assert "media://telegram/voice/voice_file_id_1234567890" in captured["content"]
    assert "[transcription: voice text]" in captured["content"]
    assert len(captured["media"]) == 1
    assert captured["metadata"]["media_refs"] == ["media://telegram/voice/voice_file_id_1234567890"]



import asyncio
import json
from pathlib import Path

from zen_claw.bus.queue import MessageBus
from zen_claw.channels.whatsapp import WhatsAppChannel
from zen_claw.config.schema import WhatsAppConfig


def test_extract_media_refs_from_single_ref() -> None:
    ch = WhatsAppChannel(WhatsAppConfig(), MessageBus())
    out = ch._extract_media_refs({"mediaType": "image", "mediaRef": "abc"})
    assert out == ["media://whatsapp/image/abc"]


def test_extract_media_refs_from_list() -> None:
    ch = WhatsAppChannel(WhatsAppConfig(), MessageBus())
    out = ch._extract_media_refs({"mediaType": "audio", "mediaRefs": ["a1", "a2"]})
    assert out == ["media://whatsapp/audio/a1", "media://whatsapp/audio/a2"]


def test_handle_bridge_message_passes_media_refs() -> None:
    ch = WhatsAppChannel(WhatsAppConfig(), MessageBus())
    captured = {}

    async def _capture_handle_message(sender_id, chat_id, content, media=None, metadata=None):
        captured["sender_id"] = sender_id
        captured["chat_id"] = chat_id
        captured["content"] = content
        captured["media"] = media or []
        captured["metadata"] = metadata or {}

    ch._handle_message = _capture_handle_message  # type: ignore[method-assign]

    payload = {
        "type": "message",
        "sender": "12345@s.whatsapp.net",
        "pn": "12345@s.whatsapp.net",
        "content": "hello",
        "mediaType": "image",
        "mediaRef": "img_1",
        "id": "m1",
        "timestamp": 1,
        "isGroup": False,
    }
    asyncio.run(ch._handle_bridge_message(json.dumps(payload)))

    assert captured["sender_id"] == "12345"
    assert captured["chat_id"] == "12345@s.whatsapp.net"
    assert captured["content"] == "hello\n[media_ref: media://whatsapp/image/img_1]"
    assert captured["media"] == ["media://whatsapp/image/img_1"]


def test_handle_bridge_message_transcribes_voice_from_local_media(tmp_path: Path) -> None:
    ch = WhatsAppChannel(WhatsAppConfig(), MessageBus(), media_root=tmp_path / "media", groq_api_key="k")
    captured = {}

    async def _capture_handle_message(sender_id, chat_id, content, media=None, metadata=None):
        captured["sender_id"] = sender_id
        captured["chat_id"] = chat_id
        captured["content"] = content
        captured["media"] = media or []
        captured["metadata"] = metadata or {}

    class _StubTranscriber:
        async def transcribe(self, file_path):
            assert Path(file_path).exists()
            return "voice text"

    ch._handle_message = _capture_handle_message  # type: ignore[method-assign]
    ch._transcriber = _StubTranscriber()  # type: ignore[attr-defined]

    audio = tmp_path / "bridge-audio.ogg"
    audio.write_bytes(b"dummy")
    payload = {
        "type": "message",
        "sender": "12345@s.whatsapp.net",
        "pn": "12345@s.whatsapp.net",
        "content": "[Voice Message]",
        "mediaType": "audio",
        "localMediaPath": str(audio),
        "id": "m2",
        "timestamp": 2,
        "isGroup": False,
    }

    asyncio.run(ch._handle_bridge_message(json.dumps(payload)))

    assert '[Voice]: "voice text"' in captured["content"]
    assert captured["media"] == ["media://local/whatsapp/bridge-audio.ogg"]
    imported = tmp_path / "media" / "whatsapp" / "bridge-audio.ogg"
    assert imported.exists()


def test_handle_bridge_message_voice_without_local_media_degrades_cleanly() -> None:
    ch = WhatsAppChannel(WhatsAppConfig(), MessageBus())
    captured = {}

    async def _capture_handle_message(sender_id, chat_id, content, media=None, metadata=None):
        captured["content"] = content
        captured["media"] = media or []

    ch._handle_message = _capture_handle_message  # type: ignore[method-assign]

    payload = {
        "type": "message",
        "sender": "12345@s.whatsapp.net",
        "pn": "12345@s.whatsapp.net",
        "content": "[Voice Message]",
        "mediaType": "audio",
        "id": "m3",
        "timestamp": 3,
        "isGroup": False,
    }
    asyncio.run(ch._handle_bridge_message(json.dumps(payload)))

    assert captured["content"] == "[Voice Message: Transcription unavailable]"
    assert captured["media"] == []


def test_handle_bridge_status_updates_connection_state() -> None:
    ch = WhatsAppChannel(WhatsAppConfig(), MessageBus())
    ch._connected = True

    asyncio.run(ch._handle_bridge_message(json.dumps({"type": "status", "status": "reconnecting"})))
    assert ch._connected is False

    asyncio.run(ch._handle_bridge_message(json.dumps({"type": "status", "status": "connected"})))
    assert ch._connected is True

    asyncio.run(
        ch._handle_bridge_message(json.dumps({"type": "status", "status": "audio_download_failed"}))
    )
    assert ch._connected is True

import asyncio
import json

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



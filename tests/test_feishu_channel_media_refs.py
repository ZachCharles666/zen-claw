import asyncio
from types import SimpleNamespace

from zen_claw.bus.queue import MessageBus
from zen_claw.channels.feishu import FeishuChannel
from zen_claw.config.schema import FeishuConfig


def _mk_event(msg_type: str, content: str, message_id: str = "m1"):
    sender = SimpleNamespace(sender_type="user", sender_id=SimpleNamespace(open_id="ou_x"))
    message = SimpleNamespace(
        message_id=message_id,
        chat_id="oc_x",
        chat_type="group",
        message_type=msg_type,
        content=content,
    )
    return SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))


def test_extract_media_refs_for_image() -> None:
    ch = FeishuChannel(FeishuConfig(), MessageBus())
    out = ch._extract_media_refs("image", '{"image_key":"img_123"}')
    assert out == [{"key": "img_123", "uri": "media://feishu/image/img_123"}]


def test_extract_media_refs_for_audio_video_file() -> None:
    ch = FeishuChannel(FeishuConfig(), MessageBus())
    assert ch._extract_media_refs("audio", '{"file_key":"aud_1"}') == [{"key": "aud_1", "uri": "media://feishu/audio/aud_1"}]
    assert ch._extract_media_refs("video", '{"file_key":"vid_1"}') == [{"key": "vid_1", "uri": "media://feishu/video/vid_1"}]
    assert ch._extract_media_refs("file", '{"file_key":"f_1"}') == [{"key": "f_1", "uri": "media://feishu/file/f_1"}]


def test_on_message_for_image_passes_media_refs() -> None:
    ch = FeishuChannel(FeishuConfig(), MessageBus())
    captured = {}

    async def _noop_reaction(message_id: str, emoji_type: str = "THUMBSUP"):
        return None

    async def _capture_handle_message(sender_id, chat_id, content, media=None, metadata=None):
        captured["sender_id"] = sender_id
        captured["chat_id"] = chat_id
        captured["content"] = content
        captured["media"] = media or []
        captured["metadata"] = metadata or {}

    async def _mock_download(message_id, file_key, msg_type):
        return f"media://local/feishu/feishu_{message_id}_{file_key}.png"

    ch._add_reaction = _noop_reaction  # type: ignore[method-assign]
    ch._handle_message = _capture_handle_message  # type: ignore[method-assign]
    ch.download_media = _mock_download  # type: ignore[method-assign]
    evt = _mk_event("image", '{"image_key":"img_123"}', "m2")
    asyncio.run(ch._on_message(evt))

    assert captured["sender_id"] == "ou_x"
    assert captured["chat_id"] == "oc_x"
    assert captured["content"] == "[image]\n[media_ref: media://local/feishu/feishu_m2_img_123.png]"
    assert captured["media"] == ["media://local/feishu/feishu_m2_img_123.png"]
    assert captured["metadata"]["msg_type"] == "image"



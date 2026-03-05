import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dingtalk_stream import CallbackMessage

from zen_claw.bus.events import OutboundMessage
from zen_claw.bus.queue import MessageBus
from zen_claw.channels.dingtalk import DingTalkChannel
from zen_claw.config.schema import DingTalkConfig


@pytest.fixture
def bus():
    return MessageBus()


@pytest.fixture
def config():
    conf = DingTalkConfig()
    conf.app_key = "test_key"
    conf.app_secret = "test_secret"
    conf.webhook_url = "https://oapi.dingtalk.com/robot/send?access_token=123"
    return conf


@pytest.mark.asyncio
async def test_dingtalk_stream_message(config, bus):
    channel = DingTalkChannel(config, bus)

    # Needs to be mocked so it actually registers the task in the mock bus
    published_msgs = []

    async def mock_handle_message(**kwargs):
        published_msgs.append(kwargs)

    channel._handle_message = mock_handle_message  # type: ignore
    channel._loop = asyncio.get_running_loop()

    # Create mock callback message
    msg = MagicMock(spec=CallbackMessage)
    msg.data = {
        "msgtype": "text",
        "senderStaffId": "staff_1",
        "conversationId": "chat_1",
        "text": {"content": "Hello DingTalk"},
    }

    # Simulate receiving message
    channel._on_stream_message(msg)

    # Yield control to event loop so asyncio.run_coroutine_threadsafe executes
    await asyncio.sleep(0.1)

    assert len(published_msgs) == 1
    assert published_msgs[0]["sender_id"] == "staff_1"
    assert published_msgs[0]["chat_id"] == "chat_1"
    assert published_msgs[0]["content"] == "Hello DingTalk"
    assert published_msgs[0]["metadata"]["mode"] == "stream"


@pytest.mark.asyncio
async def test_dingtalk_webhook_media_parsing(config, bus):
    channel = DingTalkChannel(config, bus)

    # Replace the actual publish method
    published_msgs = []

    async def mock_handle_message(**kwargs):
        published_msgs.append(kwargs)

    channel._handle_message = mock_handle_message  # type: ignore

    # Test text
    await channel.handle_webhook(
        {
            "msgtype": "picture",
            "senderStaffId": "staff_2",
            "content": {"downloadCode": "img_dl_code"},
        }
    )

    assert len(published_msgs) == 1
    assert published_msgs[0]["content"] == "[image]\n[media_ref: img_dl_code]"
    assert published_msgs[0]["metadata"]["mode"] == "webhook"


@pytest.mark.asyncio
async def test_dingtalk_send_with_media_text(config, bus):
    channel = DingTalkChannel(config, bus)

    with patch("zen_claw.channels.dingtalk.httpx.AsyncClient") as mock_client:
        mock_post = AsyncMock()
        mock_client.return_value.__aenter__.return_value.post = mock_post

        msg = OutboundMessage(
            chat_id="chat_1",
            channel="dingtalk",
            content="Look at this",
            media=["media://local/img_name.jpg"],
        )

        await channel.send(msg)

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args[1]

        assert call_kwargs["json"]["msgtype"] == "markdown"
        assert "Look at this" in call_kwargs["json"]["markdown"]["text"]
        assert (
            "[Media attached: media://local/img_name.jpg]"
            in call_kwargs["json"]["markdown"]["text"]
        )


# ── tests: MEDIUM-010 — run_in_executor Future stored and callback registered ─


@pytest.mark.asyncio
async def test_start_stores_executor_future(config, bus):
    """run_in_executor result must be stored in _executor_future (MEDIUM-010)."""
    channel = DingTalkChannel(config, bus)

    mock_client = MagicMock()

    async def _stop_soon():
        await asyncio.sleep(0.05)
        await channel.stop()

    with patch("zen_claw.channels.dingtalk.DingTalkStreamClient", return_value=mock_client):
        with patch("zen_claw.channels.dingtalk.DINGTALK_STREAM_AVAILABLE", True):
            asyncio.create_task(_stop_soon())
            await channel.start()

    assert channel._executor_future is not None


@pytest.mark.asyncio
async def test_running_set_false_when_executor_thread_exits(config, bus):
    """When the executor thread finishes, _running must be set to False (MEDIUM-010)."""
    channel = DingTalkChannel(config, bus)

    mock_client = MagicMock()
    mock_client.start = MagicMock(return_value=None)  # returns immediately

    async def _stop_soon():
        await asyncio.sleep(0.1)
        await channel.stop()

    with patch("zen_claw.channels.dingtalk.DingTalkStreamClient", return_value=mock_client):
        with patch("zen_claw.channels.dingtalk.DINGTALK_STREAM_AVAILABLE", True):
            asyncio.create_task(_stop_soon())
            await channel.start()

    await asyncio.sleep(0.05)
    assert channel._running is False


@pytest.mark.asyncio
async def test_done_callback_logs_warning_on_clean_exit(config, bus):
    """A clean thread exit must produce a logger.warning call (MEDIUM-010)."""
    channel = DingTalkChannel(config, bus)

    mock_client = MagicMock()
    mock_client.start = MagicMock(return_value=None)

    async def _stop_soon():
        await asyncio.sleep(0.1)
        await channel.stop()

    with patch("zen_claw.channels.dingtalk.DingTalkStreamClient", return_value=mock_client):
        with patch("zen_claw.channels.dingtalk.DINGTALK_STREAM_AVAILABLE", True):
            with patch("zen_claw.channels.dingtalk.logger") as mock_logger:
                asyncio.create_task(_stop_soon())
                await channel.start()
                await asyncio.sleep(0.05)

    warning_calls = " ".join(
        str(a) for call in mock_logger.warning.call_args_list for a in call.args
    )
    assert "exit" in warning_calls.lower() or "stream" in warning_calls.lower()

import asyncio

from zen_claw.bus.events import OutboundMessage
from zen_claw.bus.queue import MessageBus
from zen_claw.channels.webchat import WebChatChannel
from zen_claw.config.schema import WebChatConfig


def test_webchat_channel_ingest_and_outbound_queue() -> None:
    async def _run() -> None:
        bus = MessageBus()
        channel = WebChatChannel(WebChatConfig(enabled=True), bus)
        channel.access_checker = lambda *_args, **_kwargs: True
        await channel.start()

        await channel.ingest_user_message(
            session_id="sess-1",
            sender_id="user-1",
            content="hello",
        )
        inbound = await bus.consume_inbound()
        assert inbound.channel == "webchat"
        assert inbound.chat_id == "sess-1"
        assert inbound.content == "hello"

        msg = OutboundMessage(channel="webchat", chat_id="sess-1", content="world")
        await channel.send(msg)
        popped = await channel.pop_response("sess-1", timeout_sec=0.1)
        assert popped is not None
        assert popped.content == "world"
        await channel.stop()

    asyncio.run(_run())

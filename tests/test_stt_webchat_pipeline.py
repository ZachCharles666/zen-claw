import asyncio

from zen_claw.bus.queue import MessageBus
from zen_claw.channels.webchat import WebChatChannel
from zen_claw.config.schema import WebChatConfig


def test_webchat_audio_pipeline_transcribes_into_content(tmp_path) -> None:
    async def _run() -> None:
        bus = MessageBus()
        channel = WebChatChannel(WebChatConfig(enabled=True), bus)
        channel.access_checker = lambda *_args, **_kwargs: True

        class _StubTranscriber:
            async def transcribe(self, file_path):
                return "hello voice"

        channel._transcriber = _StubTranscriber()  # type: ignore[attr-defined]
        await channel.start()
        audio = tmp_path / "clip.ogg"
        audio.write_bytes(b"dummy")

        await channel.ingest_user_message(
            session_id="sess-voice",
            sender_id="u1",
            content="",
            media=[str(audio)],
        )
        inbound = await bus.consume_inbound()
        assert '[Voice]: "hello voice"' in inbound.content
        await channel.stop()

    asyncio.run(_run())

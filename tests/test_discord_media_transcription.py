import asyncio
from pathlib import Path

from zen_claw.bus.queue import MessageBus
from zen_claw.channels.discord import DiscordChannel
from zen_claw.config.schema import DiscordConfig


class FakeTranscriber:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key

    async def transcribe(self, file_path: str | Path) -> str:
        return "hello from audio"


def test_discord_maybe_transcribe_media_for_audio(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("zen_claw.providers.transcription.GroqTranscriptionProvider", FakeTranscriber)
    ch = DiscordChannel(DiscordConfig(), MessageBus(), media_root=tmp_path, groq_api_key="k")
    media = tmp_path / "a.mp3"
    media.write_bytes(b"x")
    out = asyncio.run(ch._maybe_transcribe_media(media, "audio/mpeg"))
    assert out == "hello from audio"


def test_discord_maybe_transcribe_media_skips_without_key(tmp_path: Path) -> None:
    ch = DiscordChannel(DiscordConfig(), MessageBus(), media_root=tmp_path, groq_api_key="")
    media = tmp_path / "a.mp3"
    media.write_bytes(b"x")
    out = asyncio.run(ch._maybe_transcribe_media(media, "audio/mpeg"))
    assert out == ""


def test_discord_maybe_transcribe_media_skips_non_av_types(tmp_path: Path) -> None:
    ch = DiscordChannel(DiscordConfig(), MessageBus(), media_root=tmp_path, groq_api_key="k")
    media = tmp_path / "a.txt"
    media.write_text("x", encoding="utf-8")
    out = asyncio.run(ch._maybe_transcribe_media(media, "text/plain"))
    assert out == ""



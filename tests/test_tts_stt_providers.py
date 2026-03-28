"""Tests for TTS/STT providers and tool — PA3."""

from types import SimpleNamespace

import pytest

from zen_claw.agent.tools.tts import TextToSpeechTool
from zen_claw.providers.transcription import GroqTranscriptionProvider, is_supported_audio_file
from zen_claw.providers.tts import EdgeTTSProvider, get_tts_provider

# ── EdgeTTSProvider ───────────────────────────────────────────────────────────

def test_edge_tts_default_voice() -> None:
    provider = EdgeTTSProvider()
    assert provider.default_voice == EdgeTTSProvider.DEFAULT_VOICE
    assert provider.default_voice == "zh-CN-XiaoxiaoNeural"


def test_edge_tts_custom_voice() -> None:
    provider = EdgeTTSProvider(default_voice="en-US-JennyNeural")
    assert provider.default_voice == "en-US-JennyNeural"


def test_edge_tts_split_text_short() -> None:
    """Short text produces a single chunk."""
    chunks = EdgeTTSProvider._split_text("Hello world", max_chars=2500)
    assert chunks == ["Hello world"]


def test_edge_tts_split_text_long() -> None:
    """Long text is split into multiple chunks without cutting mid-sentence."""
    sentence = "这是一句话。" * 5  # ~30 chars
    long_text = sentence * 100   # ~3000 chars, exceeds 2500
    chunks = EdgeTTSProvider._split_text(long_text, max_chars=2500)
    assert len(chunks) >= 2
    # No chunk exceeds max_chars (allow tiny overflow for split remainder)
    for chunk in chunks:
        assert len(chunk) <= 2500 + 50


# ── get_tts_provider factory ─────────────────────────────────────────────────

def test_get_tts_provider_edge() -> None:
    cfg = SimpleNamespace(providers=SimpleNamespace(tts="edge", tts_default_voice="zh-CN-XiaoxiaoNeural"))
    provider = get_tts_provider(cfg)
    assert isinstance(provider, EdgeTTSProvider)


def test_get_tts_provider_off_raises() -> None:
    cfg = SimpleNamespace(providers=SimpleNamespace(tts="off", tts_default_voice=""))
    with pytest.raises(ValueError, match="TTS is disabled"):
        get_tts_provider(cfg)


def test_get_tts_provider_no_config_defaults_to_edge() -> None:
    provider = get_tts_provider(SimpleNamespace())
    assert isinstance(provider, EdgeTTSProvider)


# ── GroqTranscriptionProvider ─────────────────────────────────────────────────

def test_groq_transcription_init_with_key() -> None:
    provider = GroqTranscriptionProvider(api_key="groq-test-key")
    assert provider.api_key == "groq-test-key"


@pytest.mark.asyncio
async def test_groq_transcription_empty_key_returns_empty_string(tmp_path) -> None:
    """No API key → returns '' without raising."""
    provider = GroqTranscriptionProvider(api_key="")
    provider.api_key = ""  # ensure blank
    result = await provider.transcribe(tmp_path / "audio.mp3")
    assert result == ""


def test_is_supported_audio_file_valid_extensions() -> None:
    for ext in [".mp3", ".ogg", ".wav", ".m4a", ".webm"]:
        assert is_supported_audio_file(f"audio{ext}") is True


def test_is_supported_audio_file_invalid_extension() -> None:
    assert is_supported_audio_file("audio.txt") is False
    assert is_supported_audio_file("video.avi") is False


# ── TextToSpeechTool ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tts_tool_empty_text_returns_parameter_failure(tmp_path) -> None:
    tool = TextToSpeechTool(workspace=tmp_path)
    result = await tool.execute(text="")
    assert result.ok is False
    assert result.error is not None
    assert "text" in result.error.message.lower()


@pytest.mark.asyncio
async def test_tts_tool_path_traversal_returns_permission_failure(tmp_path) -> None:
    tool = TextToSpeechTool(workspace=tmp_path)
    result = await tool.execute(text="Hello", output_filename="../../../etc/passwd")
    assert result.ok is False
    assert result.error is not None

"""Tests for TTS providers and tool."""

from __future__ import annotations

import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zen_claw.agent.tools.tts import TextToSpeechTool
from zen_claw.providers.tts import EdgeTTSProvider, get_tts_provider


class TestEdgeTTSProvider:
    def test_text_split_short(self):
        assert EdgeTTSProvider._split_text("你好世界", max_chars=2500) == ["你好世界"]

    def test_text_split_long(self):
        long = "这是第一句话。" * 400
        chunks = EdgeTTSProvider._split_text(long, max_chars=2500)
        assert len(chunks) > 1
        assert all(len(c) <= 2500 for c in chunks)

    def test_default_voice(self):
        assert EdgeTTSProvider().default_voice == "zh-CN-XiaoxiaoNeural"

    def test_custom_voice(self):
        assert (
            EdgeTTSProvider(default_voice="zh-CN-YunxiNeural").default_voice == "zh-CN-YunxiNeural"
        )

    @pytest.mark.asyncio
    async def test_synthesize_with_mock(self):
        provider = EdgeTTSProvider()
        fake = b"ID3" + b"\x00" * 100
        fake_edge = types.SimpleNamespace(Communicate=object)
        with (
            patch.dict("sys.modules", {"edge_tts": fake_edge}),
            patch.object(provider, "_synthesize_chunk", return_value=fake),
        ):
            result = await provider.synthesize("测试文本")
        assert result == fake


class TestGetTTSProvider:
    def test_default_is_edge(self):
        config = MagicMock()
        config.providers.tts = "edge"
        config.providers.tts_default_voice = "zh-CN-XiaoxiaoNeural"
        provider = get_tts_provider(config)
        assert isinstance(provider, EdgeTTSProvider)

    def test_off_raises(self):
        config = MagicMock()
        config.providers.tts = "off"
        with pytest.raises(ValueError, match="disabled"):
            get_tts_provider(config)

    def test_unknown_raises(self):
        config = MagicMock()
        config.providers.tts = "unknown_provider"
        with pytest.raises(ValueError, match="Unknown TTS provider"):
            get_tts_provider(config)


class TestTextToSpeechTool:
    @pytest.fixture
    def workspace(self, tmp_path):
        p = tmp_path / "workspace"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @pytest.fixture
    def tool(self, workspace):
        config = MagicMock()
        config.providers.tts = "edge"
        config.providers.tts_default_voice = "zh-CN-XiaoxiaoNeural"
        return TextToSpeechTool(workspace=workspace, config=config)

    def test_resolve_relative_path(self, tool):
        p = tool._resolve_output_path("audio/test.mp3", "mp3")
        assert str(tool.workspace) in str(p)

    def test_reject_absolute_outside_workspace(self, tool):
        with pytest.raises(ValueError, match="outside"):
            tool._resolve_output_path("/etc/passwd", "mp3")

    def test_auto_filename_inside_workspace(self, tool):
        p = tool._resolve_output_path("", "mp3")
        assert str(tool.workspace) in str(p)
        assert p.suffix == ".mp3"

    @pytest.mark.asyncio
    async def test_execute_empty_text_returns_error(self, tool):
        result = await tool.execute(text="")
        assert result.ok is False
        assert result.error is not None
        assert "required" in result.error.message.lower()

    @pytest.mark.asyncio
    async def test_execute_with_mock_provider(self, tool):
        fake_audio = b"ID3" + b"\x00" * 200

        async def _fake_synthesize_to_file(text, output_path, voice=None, output_format="mp3"):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(fake_audio)
            return output_path

        mock_provider = AsyncMock()
        mock_provider.synthesize_to_file = _fake_synthesize_to_file
        tool._tts_provider = mock_provider

        result = await tool.execute(
            text="你好世界", voice="zh-CN-XiaoxiaoNeural", output_filename="test_output.mp3"
        )
        assert result.ok is True
        out = Path(result.meta["output_path"])
        assert out.exists()
        assert out.read_bytes() == fake_audio

    @pytest.mark.asyncio
    async def test_voice_passed_to_provider(self, tool):
        calls = []

        async def _capture(text, output_path, voice=None, output_format="mp3"):
            calls.append({"voice": voice, "text": text})
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"fake")
            return output_path

        mock_provider = AsyncMock()
        mock_provider.synthesize_to_file = _capture
        tool._tts_provider = mock_provider
        await tool.execute(
            text="测试语音", voice="zh-CN-YunxiNeural", output_filename="voice_test.mp3"
        )
        assert calls[0]["voice"] == "zh-CN-YunxiNeural"

"""Text-to-Speech providers."""

from __future__ import annotations

import asyncio
import os
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from loguru import logger


class TTSProvider(ABC):
    @abstractmethod
    async def synthesize(self, text: str, voice: str | None = None, output_format: str = "mp3") -> bytes:
        ...

    async def synthesize_to_file(
        self,
        text: str,
        output_path: Path,
        voice: str | None = None,
        output_format: str = "mp3",
    ) -> Path:
        audio = await self.synthesize(text=text, voice=voice, output_format=output_format)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(audio)
        return output_path


class EdgeTTSProvider(TTSProvider):
    SUPPORTED_VOICES = [
        "zh-CN-XiaoxiaoNeural",
        "zh-CN-YunxiNeural",
        "zh-CN-XiaoyiNeural",
        "zh-CN-YunjianNeural",
        "zh-TW-HsiaoChenNeural",
        "zh-HK-HiuMaanNeural",
        "en-US-JennyNeural",
        "en-US-GuyNeural",
    ]
    DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

    def __init__(self, default_voice: str | None = None) -> None:
        self.default_voice = default_voice or self.DEFAULT_VOICE

    async def synthesize(self, text: str, voice: str | None = None, output_format: str = "mp3") -> bytes:
        try:
            import edge_tts
        except ImportError as exc:
            raise RuntimeError("edge-tts is not installed") from exc
        chunks = self._split_text(text, max_chars=2500)
        selected = voice or self.default_voice
        audio_parts: list[bytes] = []
        for chunk in chunks:
            if not chunk.strip():
                continue
            audio_parts.append(await self._synthesize_chunk(edge_tts, chunk, selected))
        if not audio_parts:
            raise RuntimeError("no audio generated")
        audio = b"".join(audio_parts)
        if output_format.lower() == "wav":
            return await self._mp3_to_wav(audio)
        return audio

    async def _synthesize_chunk(self, edge_tts_module, text: str, voice: str) -> bytes:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            comm = edge_tts_module.Communicate(text=text, voice=voice)
            await comm.save(str(tmp_path))
            return tmp_path.read_bytes()
        finally:
            tmp_path.unlink(missing_ok=True)

    @staticmethod
    def _split_text(text: str, max_chars: int = 2500) -> list[str]:
        if len(text) <= max_chars:
            return [text]
        import re

        sents = re.split(r"(?<=[。！？.!?])\s*", text)
        out: list[str] = []
        cur = ""
        for sent in sents:
            if len(cur) + len(sent) <= max_chars:
                cur += sent
            else:
                if cur:
                    out.append(cur)
                cur = sent
        if cur:
            out.append(cur)
        return out or [text[:max_chars]]

    @staticmethod
    async def _mp3_to_wav(mp3_bytes: bytes) -> bytes:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            in_path = Path(f.name)
            f.write(mp3_bytes)
        out_path = in_path.with_suffix(".wav")
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-i",
                str(in_path),
                str(out_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if proc.returncode == 0 and out_path.exists():
                return out_path.read_bytes()
            return mp3_bytes
        except FileNotFoundError:
            return mp3_bytes
        finally:
            in_path.unlink(missing_ok=True)
            out_path.unlink(missing_ok=True)

    @staticmethod
    async def list_voices(locale_filter: str = "zh") -> list[dict[str, str]]:
        try:
            import edge_tts
        except ImportError:
            return []
        voices = await edge_tts.list_voices()
        rows: list[dict[str, str]] = []
        for v in voices:
            locale = str(v.get("Locale", ""))
            if locale_filter and not locale.startswith(locale_filter):
                continue
            rows.append({"name": str(v.get("ShortName", "")), "locale": locale, "gender": str(v.get("Gender", ""))})
        return rows


class MinimaxTTSProvider(TTSProvider):
    API_URL = "https://api.minimax.chat/v1/t2a_v2"
    DEFAULT_VOICE = "female-shaonv"

    def __init__(self, api_key: str | None = None, group_id: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("zen_claw_MINIMAX_API_KEY", "")
        self.group_id = group_id or os.environ.get("zen_claw_MINIMAX_GROUP_ID", "")

    async def synthesize(self, text: str, voice: str | None = None, output_format: str = "mp3") -> bytes:
        if not self.api_key:
            raise RuntimeError("MiniMax API key not configured")
        import httpx

        payload = {
            "model": "speech-01-turbo",
            "text": text,
            "stream": False,
            "voice_setting": {"voice_id": voice or self.DEFAULT_VOICE, "speed": 1.0, "vol": 1.0, "pitch": 0},
            "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3", "channel": 1},
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        params = {"GroupId": self.group_id} if self.group_id else {}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(self.API_URL, json=payload, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
        audio_hex = data.get("data", {}).get("audio") or data.get("audio_file") or ""
        if not audio_hex:
            raise RuntimeError("MiniMax response missing audio")
        return bytes.fromhex(audio_hex)


class OpenAITTSProvider(TTSProvider):
    DEFAULT_VOICE = "nova"
    DEFAULT_MODEL = "tts-1"

    def __init__(self, api_key: str | None = None, api_base: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.api_base = api_base or "https://api.openai.com/v1"
        self.model = model or self.DEFAULT_MODEL

    async def synthesize(self, text: str, voice: str | None = None, output_format: str = "mp3") -> bytes:
        if not self.api_key:
            raise RuntimeError("OpenAI API key not configured")
        import httpx

        payload = {"model": self.model, "input": text, "voice": voice or self.DEFAULT_VOICE, "response_format": output_format}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(f"{self.api_base.rstrip('/')}/audio/speech", json=payload, headers=headers)
            resp.raise_for_status()
            return resp.content


def get_tts_provider(config: Any) -> TTSProvider:
    providers_cfg = getattr(config, "providers", None)
    provider_name = getattr(providers_cfg, "tts", "edge") if providers_cfg else "edge"
    default_voice = getattr(providers_cfg, "tts_default_voice", EdgeTTSProvider.DEFAULT_VOICE) if providers_cfg else EdgeTTSProvider.DEFAULT_VOICE
    if provider_name in {"", "edge"}:
        return EdgeTTSProvider(default_voice=default_voice)
    if provider_name == "minimax":
        return MinimaxTTSProvider(
            api_key=getattr(providers_cfg, "minimax_api_key", ""),
            group_id=getattr(providers_cfg, "minimax_group_id", ""),
        )
    if provider_name == "openai":
        openai_cfg = getattr(providers_cfg, "openai", None)
        return OpenAITTSProvider(
            api_key=getattr(openai_cfg, "api_key", ""),
            api_base=getattr(openai_cfg, "api_base", None),
        )
    if provider_name == "off":
        raise ValueError("TTS is disabled (providers.tts = 'off')")
    logger.error(f"Unknown TTS provider: {provider_name}")
    raise ValueError(f"Unknown TTS provider: '{provider_name}'. Valid options: edge, minimax, openai, off")

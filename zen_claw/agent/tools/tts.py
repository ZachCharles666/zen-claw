"""Text to speech agent tool."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from loguru import logger

from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult


class TextToSpeechTool(Tool):
    name = "text_to_speech"
    description = "Convert text to speech and save as audio file in workspace."
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "voice": {"type": "string", "default": ""},
            "output_filename": {"type": "string", "default": ""},
            "output_format": {"type": "string", "enum": ["mp3", "wav"], "default": "mp3"},
        },
        "required": ["text"],
    }

    def __init__(self, workspace: Path, config: Any = None) -> None:
        self.workspace = Path(workspace).resolve()
        self.config = config
        self._tts_provider = None

    async def _get_provider(self):
        if self._tts_provider is None:
            from zen_claw.providers.tts import get_tts_provider

            self._tts_provider = get_tts_provider(self.config)
        return self._tts_provider

    def _resolve_output_path(self, output_filename: str, output_format: str) -> Path:
        if not output_filename:
            output_filename = f"tts/tts_{int(time.time())}.{output_format}"
        requested = Path(output_filename)
        resolved = requested if requested.is_absolute() else (self.workspace / requested)
        resolved = resolved.resolve()
        try:
            resolved.relative_to(self.workspace)
        except ValueError as exc:
            raise ValueError(
                f"Output path '{output_filename}' is outside the workspace boundary."
            ) from exc
        return resolved

    async def execute(
        self,
        text: str,
        voice: str = "",
        output_filename: str = "",
        output_format: str = "mp3",
        **kwargs: Any,
    ) -> ToolResult:
        if not text or not text.strip():
            return ToolResult.failure(
                ToolErrorKind.PARAMETER, "text parameter is required and cannot be empty"
            )
        if len(text) > 5000:
            text = text[:5000]
        try:
            output_path = self._resolve_output_path(output_filename, output_format)
        except ValueError as exc:
            return ToolResult.failure(ToolErrorKind.PERMISSION, str(exc))
        try:
            provider = await self._get_provider()
            output_path = await provider.synthesize_to_file(
                text=text,
                output_path=output_path,
                voice=voice or None,
                output_format=output_format,
            )
            return ToolResult.success(
                content=str(output_path),
                output_path=str(output_path),
                size_bytes=output_path.stat().st_size,
                format=output_format,
                voice=voice or "default",
            )
        except Exception as exc:
            logger.error(f"TTS tool error: {exc}")
            return ToolResult.failure(ToolErrorKind.RUNTIME, str(exc), code="tts_failed")

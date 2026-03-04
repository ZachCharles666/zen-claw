"""Web chat channel backed by in-process queues."""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from pathlib import Path

from zen_claw.bus.events import OutboundMessage
from zen_claw.bus.queue import MessageBus
from zen_claw.channels.base import BaseChannel
from zen_claw.config.schema import WebChatConfig
from zen_claw.providers.transcription import GroqTranscriptionProvider, is_supported_audio_file


class WebChatChannel(BaseChannel):
    """In-process web chat channel for dashboard websocket sessions."""

    name = "webchat"

    def __init__(self, config: WebChatConfig, bus: MessageBus, media_root=None):
        super().__init__(config, bus, media_root=media_root)
        self.config: WebChatConfig = config
        self._session_outbound: dict[str, asyncio.Queue[OutboundMessage]] = defaultdict(asyncio.Queue)
        self._transcriber = GroqTranscriptionProvider(api_key=os.environ.get("GROQ_API_KEY"))

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False
        self._session_outbound.clear()

    async def send(self, msg: OutboundMessage) -> None:
        await self._session_outbound[str(msg.chat_id)].put(msg)

    async def ingest_user_message(
        self,
        *,
        session_id: str,
        sender_id: str,
        content: str,
        media: list[str] | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Publish a webchat inbound message to the message bus."""
        media_items = media or []
        voice_text = await self._transcribe_first_audio(media_items)
        merged_content = content
        if voice_text:
            merged_content = f'{content}\n[Voice]: "{voice_text}"'.strip()
        meta = dict(metadata or {})
        meta.setdefault("webchat_session_id", session_id)
        await self._handle_message(
            sender_id=sender_id,
            chat_id=session_id,
            content=merged_content,
            media=media_items,
            metadata=meta,
        )

    async def _transcribe_first_audio(self, media: list[str]) -> str:
        for item in media:
            path = Path(str(item))
            if not path.exists() or not path.is_file():
                continue
            if not is_supported_audio_file(path):
                continue
            text = (await self._transcriber.transcribe(path)).strip()
            if text:
                return text
        return ""

    async def pop_response(self, session_id: str, timeout_sec: float = 0.0) -> OutboundMessage | None:
        """Pop next outbound message for a webchat session."""
        queue = self._session_outbound[str(session_id)]
        if timeout_sec <= 0:
            try:
                return queue.get_nowait()
            except asyncio.QueueEmpty:
                return None
        try:
            return await asyncio.wait_for(queue.get(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            return None

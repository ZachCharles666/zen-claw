"""WhatsApp channel implementation using Node.js bridge."""

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

from loguru import logger

from zen_claw.bus.events import OutboundMessage
from zen_claw.bus.queue import MessageBus
from zen_claw.channels.base import BaseChannel
from zen_claw.config.schema import WhatsAppConfig
from zen_claw.providers.transcription import GroqTranscriptionProvider, is_supported_audio_file
from zen_claw.utils.formatting import strip_markdown


class WhatsAppChannel(BaseChannel):
    """
    WhatsApp channel that connects to a Node.js bridge.

    The bridge uses @whiskeysockets/baileys to handle the WhatsApp Web protocol.
    Communication between Python and Node.js is via WebSocket.
    """

    name = "whatsapp"

    def __init__(
        self,
        config: WhatsAppConfig,
        bus: MessageBus,
        media_root=None,
        groq_api_key: str = "",
    ):
        super().__init__(config, bus, media_root=media_root)
        self.config: WhatsAppConfig = config
        self._ws = None
        self._connected = False
        self.groq_api_key = groq_api_key
        self._transcriber = GroqTranscriptionProvider(
            api_key=groq_api_key or os.environ.get("GROQ_API_KEY")
        )

    async def start(self) -> None:
        """Start the WhatsApp channel by connecting to the bridge."""
        import websockets

        bridge_url = self.config.bridge_url

        logger.info(f"Connecting to WhatsApp bridge at {bridge_url}...")

        self._running = True

        while self._running:
            try:
                async with websockets.connect(bridge_url) as ws:
                    self._ws = ws
                    self._connected = True
                    logger.info("Connected to WhatsApp bridge")

                    # Listen for messages
                    async for message in ws:
                        try:
                            await self._handle_bridge_message(message)
                        except Exception as e:
                            logger.error(f"Error handling bridge message: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                self._ws = None
                logger.warning(f"WhatsApp bridge connection error: {e}")

                if self._running:
                    logger.info("Reconnecting in 5 seconds...")
                    await asyncio.sleep(5)

    async def stop(self) -> None:
        """Stop the WhatsApp channel."""
        self._running = False
        self._connected = False

        if self._ws:
            await self._ws.close()
            self._ws = None

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through WhatsApp."""
        if not self._ws or not self._connected:
            logger.warning("WhatsApp bridge not connected")
            return

        try:
            content = strip_markdown(msg.content)
            payload = {"type": "send", "to": msg.chat_id, "text": content}
            await self._ws.send(json.dumps(payload))
        except Exception as e:
            logger.error(f"Error sending WhatsApp message: {e}")

    async def _handle_bridge_message(self, raw: str) -> None:
        """Handle a message from the bridge."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON from bridge: {raw[:100]}")
            return

        msg_type = data.get("type")

        if msg_type == "message":
            # Incoming message from WhatsApp
            # Deprecated by whatsapp: old phone number style typically: <phone>@s.whatspp.net
            pn = data.get("pn", "")
            # New LID sytle typically:
            sender = data.get("sender", "")
            content = data.get("content", "")

            # Extract just the phone number or lid as chat_id
            user_id = pn if pn else sender
            sender_id = user_id.split("@")[0] if "@" in user_id else user_id
            logger.info(f"Sender {sender}")

            local_media_refs = await self._import_local_media_refs(data)
            media_refs = self._extract_media_refs(data)
            if local_media_refs:
                media_refs = local_media_refs + media_refs

            if content == "[Voice Message]":
                transcription = await self._transcribe_first_audio(media_refs)
                if transcription:
                    content = f'[Voice Message]\n[Voice]: "{transcription}"'
                else:
                    logger.info(
                        f"Voice message received from {sender_id}, but transcription is unavailable."
                    )
                    content = "[Voice Message: Transcription unavailable]"

            if media_refs:
                content = (content + "\n" if content else "") + "\n".join(
                    f"[media_ref: {r}]" for r in media_refs
                )

            await self._handle_message(
                sender_id=sender_id,
                chat_id=sender,  # Use full LID for replies
                content=content,
                media=media_refs,
                metadata={
                    "message_id": data.get("id"),
                    "timestamp": data.get("timestamp"),
                    "is_group": data.get("isGroup", False),
                },
            )

        elif msg_type == "status":
            # Connection status update
            status = data.get("status")
            logger.info(f"WhatsApp status: {status}")

            if status == "connected":
                self._connected = True
            elif status in {"disconnected", "reconnecting", "starting", "qr_required"}:
                self._connected = False
            elif status == "audio_download_failed":
                logger.warning("WhatsApp bridge could not persist inbound audio for transcription")

        elif msg_type == "qr":
            # QR code for authentication
            logger.info("Scan QR code in the bridge terminal to connect WhatsApp")

        elif msg_type == "error":
            logger.error(f"WhatsApp bridge error: {data.get('error')}")

    def _extract_media_refs(self, data: dict[str, Any]) -> list[str]:
        """Extract media references from bridge payload."""
        media_type = str(data.get("mediaType", "file") or "file").strip().lower()
        raw_refs = data.get("mediaRefs")
        refs: list[str] = []
        if isinstance(raw_refs, list):
            for r in raw_refs:
                if isinstance(r, str) and r.strip():
                    refs.append(r.strip())
        else:
            r = data.get("mediaRef")
            if isinstance(r, str) and r.strip():
                refs.append(r.strip())

        out = []
        for r in refs[:8]:
            out.append(self._build_media_uri("whatsapp", media_type, r))
        return out

    async def _import_local_media_refs(self, data: dict[str, Any]) -> list[str]:
        media_type = str(data.get("mediaType", "file") or "file").strip().lower()
        raw_paths: list[str] = []
        raw_path = data.get("localMediaPath")
        if isinstance(raw_path, str) and raw_path.strip():
            raw_paths.append(raw_path.strip())
        raw_many = data.get("localMediaPaths")
        if isinstance(raw_many, list):
            for item in raw_many:
                if isinstance(item, str) and item.strip():
                    raw_paths.append(item.strip())

        refs: list[str] = []
        for item in raw_paths[:8]:
            media_ref = await self._import_bridge_media(item, media_type)
            if media_ref:
                refs.append(media_ref)
        return refs

    async def _import_bridge_media(self, raw_path: str, media_type: str) -> str:
        source = Path(str(raw_path))
        if not source.exists() or not source.is_file():
            return ""
        target_dir = self.media_root / "whatsapp"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / source.name
        if target.exists():
            same_stat = (
                target.stat().st_size == source.stat().st_size
                and target.stat().st_mtime_ns == source.stat().st_mtime_ns
            )
            if same_stat:
                return self._build_local_media_uri(target.name)
            target = target_dir / f"{target.stem}_{int(source.stat().st_mtime_ns)}{target.suffix}"
        await asyncio.to_thread(shutil.copy2, source, target)
        return self._build_local_media_uri(target.name)

    def _build_local_media_uri(self, file_name: str) -> str:
        return f"media://local/whatsapp/{file_name}"

    def _local_path_from_media_uri(self, media_uri: str) -> Path | None:
        if not media_uri.startswith("media://local/whatsapp/"):
            return None
        path = self.media_root / "whatsapp" / media_uri.rsplit("/", 1)[-1]
        return path if path.exists() else None

    async def _transcribe_first_audio(self, media_refs: list[str]) -> str:
        for media_uri in media_refs:
            path = self._local_path_from_media_uri(media_uri)
            if path is None or not is_supported_audio_file(path):
                continue
            text = (await self._transcriber.transcribe(path)).strip()
            if text:
                return text
        return ""

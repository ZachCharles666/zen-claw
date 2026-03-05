"""Feishu/Lark channel implementation using lark-oapi SDK with WebSocket long connection."""

import asyncio
import json
import os
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from loguru import logger

from zen_claw.bus.events import OutboundMessage
from zen_claw.bus.queue import MessageBus
from zen_claw.channels.base import BaseChannel
from zen_claw.config.schema import FeishuConfig
from zen_claw.providers.transcription import GroqTranscriptionProvider, is_supported_audio_file

try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import (
        CreateMessageReactionRequest,
        CreateMessageReactionRequestBody,
        CreateMessageRequest,
        CreateMessageRequestBody,
        Emoji,
        P2ImMessageReceiveV1,
    )

    FEISHU_AVAILABLE = True
except ImportError:
    FEISHU_AVAILABLE = False
    lark = None
    Emoji = None

# Message type display mapping
MSG_TYPE_MAP = {
    "image": "[image]",
    "audio": "[audio]",
    "video": "[video]",
    "file": "[file]",
    "sticker": "[sticker]",
}


class FeishuChannel(BaseChannel):
    """
    Feishu/Lark channel using WebSocket long connection.

    Uses WebSocket to receive events - no public IP or webhook required.

    Requires:
    - App ID and App Secret from Feishu Open Platform
    - Bot capability enabled
    - Event subscription enabled (im.message.receive_v1)
    """

    name = "feishu"

    def __init__(self, config: FeishuConfig, bus: MessageBus, media_root=None):
        super().__init__(config, bus, media_root=media_root)
        self.config: FeishuConfig = config
        self.groq_api_key = os.environ.get("GROQ_API_KEY", "")
        self._client: Any = None
        self._ws_client: Any = None
        self._ws_thread: threading.Thread | None = None
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()  # Ordered dedup cache
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        """Start the Feishu bot with WebSocket long connection."""
        if not FEISHU_AVAILABLE:
            logger.error("Feishu SDK not installed. Run: pip install lark-oapi")
            return

        if not self.config.app_id or not self.config.app_secret:
            logger.error("Feishu app_id and app_secret not configured")
            return

        self._running = True
        self._loop = asyncio.get_running_loop()

        # Create Lark client for sending messages
        self._client = (
            lark.Client.builder()
            .app_id(self.config.app_id)
            .app_secret(self.config.app_secret)
            .log_level(lark.LogLevel.INFO)
            .build()
        )

        # Create event handler (only register message receive, ignore other events)
        event_handler = (
            lark.EventDispatcherHandler.builder(
                self.config.encrypt_key or "",
                self.config.verification_token or "",
            )
            .register_p2_im_message_receive_v1(self._on_message_sync)
            .build()
        )

        # Create WebSocket client for long connection
        self._ws_client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )

        # Start WebSocket client in a separate thread
        def run_ws():
            try:
                self._ws_client.start()
            except Exception as e:
                logger.error(f"Feishu WebSocket error: {e}")

        self._ws_thread = threading.Thread(target=run_ws, daemon=True)
        self._ws_thread.start()

        logger.info("Feishu bot started with WebSocket long connection")
        logger.info("No public IP required - using WebSocket to receive events")

        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """Stop the Feishu bot."""
        self._running = False
        if self._ws_client:
            try:
                self._ws_client.stop()
            except Exception as e:
                logger.warning(f"Error stopping WebSocket client: {e}")
        logger.info("Feishu bot stopped")

    def _add_reaction_sync(self, message_id: str, emoji_type: str) -> None:
        """Sync helper for adding reaction (runs in thread pool)."""
        try:
            request = (
                CreateMessageReactionRequest.builder()
                .message_id(message_id)
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                    .build()
                )
                .build()
            )

            response = self._client.im.v1.message_reaction.create(request)

            if not response.success():
                logger.warning(f"Failed to add reaction: code={response.code}, msg={response.msg}")
            else:
                logger.debug(f"Added {emoji_type} reaction to message {message_id}")
        except Exception as e:
            logger.warning(f"Error adding reaction: {e}")

    async def _add_reaction(self, message_id: str, emoji_type: str = "THUMBSUP") -> None:
        """
        Add a reaction emoji to a message (non-blocking).

        Common emoji types: THUMBSUP, OK, EYES, DONE, OnIt, HEART
        """
        if not self._client or not Emoji:
            return

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._add_reaction_sync, message_id, emoji_type)

    def _download_media_sync(self, message_id: str, file_key: str, msg_type: str) -> str | None:
        """Download Feishu media synchronously."""
        try:
            from lark_oapi.api.im.v1 import GetMessageResourceRequest

            # The API asks for "image", "file", etc as type
            api_type = msg_type if msg_type in ("image", "file") else "file"

            request = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(file_key)
                .type(api_type)
                .build()
            )

            response = self._client.im.v1.message_resource.get(request)
            if not response.success():
                logger.warning(f"Feishu media download failed: {response.code} {response.msg}")
                return None

            if not response.file:
                logger.warning("Feishu media download returned no file data")
                return None

            # Create the media path
            self.media_root.mkdir(parents=True, exist_ok=True)
            ext = ".png" if api_type == "image" else ".bin"
            file_name = f"feishu_{message_id}_{file_key}{ext}"
            out_path = self.media_root / file_name

            # Write to disk
            if hasattr(response.file, "read"):
                with out_path.open("wb") as f:
                    f.write(response.file.read())
            else:
                with out_path.open("wb") as f:
                    f.write(response.file)

            logger.debug(f"Downloaded Feishu media to {out_path}")
            return f"media://local/feishu/{file_name}"
        except Exception as e:
            logger.warning(f"Error downloading Feishu media {file_key}: {e}")
            return None

    async def download_media(self, message_id: str, file_key: str, msg_type: str) -> str | None:
        """Download media from Feishu asynchronously."""
        if not self._client:
            return None

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._download_media_sync, message_id, file_key, msg_type
        )

    # Regex to match markdown tables (header + separator + data rows)
    _TABLE_RE = re.compile(
        r"((?:^[ \t]*\|.+\|[ \t]*\n)(?:^[ \t]*\|[-:\s|]+\|[ \t]*\n)(?:^[ \t]*\|.+\|[ \t]*\n?)+)",
        re.MULTILINE,
    )

    @staticmethod
    def _parse_md_table(table_text: str) -> dict | None:
        """Parse a markdown table into a Feishu table element."""
        lines = [line.strip() for line in table_text.strip().split("\n") if line.strip()]
        if len(lines) < 3:
            return None

        def split_row(raw_line: str) -> list[str]:
            return [cell.strip() for cell in raw_line.strip("|").split("|")]

        headers = split_row(lines[0])
        rows = [split_row(line) for line in lines[2:]]
        columns = [
            {"tag": "column", "name": f"c{i}", "display_name": h, "width": "auto"}
            for i, h in enumerate(headers)
        ]
        return {
            "tag": "table",
            "page_size": len(rows) + 1,
            "columns": columns,
            "rows": [
                {f"c{i}": r[i] if i < len(r) else "" for i in range(len(headers))} for r in rows
            ],
        }

    def _build_card_elements(self, content: str) -> list[dict]:
        """Split content into markdown + table elements for Feishu card."""
        elements, last_end = [], 0
        for m in self._TABLE_RE.finditer(content):
            before = content[last_end : m.start()].strip()
            if before:
                elements.append({"tag": "markdown", "content": before})
            elements.append(
                self._parse_md_table(m.group(1)) or {"tag": "markdown", "content": m.group(1)}
            )
            last_end = m.end()
        remaining = content[last_end:].strip()
        if remaining:
            elements.append({"tag": "markdown", "content": remaining})
        return elements or [{"tag": "markdown", "content": content}]

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Feishu."""
        if not self._client:
            logger.warning("Feishu client not initialized")
            return

        try:
            # Determine receive_id_type based on chat_id format
            # open_id starts with "ou_", chat_id starts with "oc_"
            if msg.chat_id.startswith("oc_"):
                receive_id_type = "chat_id"
            else:
                receive_id_type = "open_id"

            # Build card with markdown + table support
            content_str = msg.content or ""

            # Process outgoing media uploads if present
            if msg.media:
                for uri in msg.media:
                    if uri.startswith("media://local/"):
                        # Extract the local path
                        local_path = self.media_root / uri.split("/")[-1]
                        if local_path.exists():
                            # If it's an image, upload as image and prepend to content
                            # Basic integration for now, sending images as markdown tags
                            try:
                                from lark_oapi.api.im.v1 import (
                                    CreateImageRequest,
                                    CreateImageRequestBody,
                                )

                                with local_path.open("rb") as f:
                                    req = (
                                        CreateImageRequest.builder()
                                        .request_body(
                                            CreateImageRequestBody.builder()
                                            .image_type("message")
                                            .image(f)
                                            .build()
                                        )
                                        .build()
                                    )
                                    resp = self._client.im.v1.image.create(req)
                                    if resp.success() and resp.data:
                                        img_key = resp.data.image_key
                                        content_str = f"![image]({img_key})\n" + content_str
                            except Exception as e:
                                logger.warning(
                                    f"Failed to upload media {local_path} to Feishu: {e}"
                                )

            elements = self._build_card_elements(content_str)
            card = {
                "config": {"wide_screen_mode": True},
                "elements": elements,
            }
            content = json.dumps(card, ensure_ascii=False)

            request = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(msg.chat_id)
                    .msg_type("interactive")
                    .content(content)
                    .build()
                )
                .build()
            )

            response = self._client.im.v1.message.create(request)

            if not response.success():
                logger.error(
                    f"Failed to send Feishu message: code={response.code}, "
                    f"msg={response.msg}, log_id={response.get_log_id()}"
                )
            else:
                logger.debug(f"Feishu message sent to {msg.chat_id}")

        except Exception as e:
            logger.error(f"Error sending Feishu message: {e}")

    def _on_message_sync(self, data: "P2ImMessageReceiveV1") -> None:
        """
        Sync handler for incoming messages (called from WebSocket thread).
        Schedules async handling in the main event loop.
        """
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._on_message(data), self._loop)

    async def _on_message(self, data: "P2ImMessageReceiveV1") -> None:
        """Handle incoming message from Feishu."""
        try:
            event = data.event
            message = event.message
            sender = event.sender

            # Deduplication check
            message_id = message.message_id
            if message_id in self._processed_message_ids:
                return
            self._processed_message_ids[message_id] = None

            # Trim cache: keep most recent 500 when exceeds 1000
            while len(self._processed_message_ids) > 1000:
                self._processed_message_ids.popitem(last=False)

            # Skip bot messages
            sender_type = sender.sender_type
            if sender_type == "bot":
                return

            sender_id = sender.sender_id.open_id if sender.sender_id else "unknown"
            chat_id = message.chat_id
            chat_type = message.chat_type  # "p2p" or "group"
            msg_type = message.message_type

            # Add reaction to indicate "seen"
            await self._add_reaction(message_id, "THUMBSUP")

            # Parse message content
            media_refs: list[str] = []
            if msg_type == "text":
                try:
                    content = json.loads(message.content).get("text", "")
                except json.JSONDecodeError:
                    content = message.content or ""
            else:
                content = MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]")
                raw_refs = self._extract_media_refs(msg_type, message.content)
                for r in raw_refs:
                    # Download the media file from Feishu
                    if r["key"]:
                        local_uri = await self.download_media(message_id, r["key"], msg_type)
                        if local_uri:
                            media_refs.append(local_uri)
                            content += f"\n[media_ref: {local_uri}]"
                            transcription = await self._maybe_transcribe_media(local_uri, msg_type)
                            if transcription:
                                content += f'\n[Voice]: "{transcription}"'
                        else:
                            content += f"\n[media_ref: {r['uri']}]"

            if not content:
                return

            # Forward to message bus
            reply_to = chat_id if chat_type == "group" else sender_id
            await self._handle_message(
                sender_id=sender_id,
                chat_id=reply_to,
                content=content,
                media=media_refs,
                metadata={
                    "message_id": message_id,
                    "chat_type": chat_type,
                    "msg_type": msg_type,
                },
            )

        except Exception as e:
            logger.error(f"Error processing Feishu message: {e}")

    def _extract_media_refs(self, msg_type: str, raw_content: str | None) -> list[dict[str, str]]:
        """Extract media reference keys for non-text Feishu messages."""
        if not raw_content:
            return []
        try:
            payload = json.loads(raw_content)
        except json.JSONDecodeError:
            return []
        key_fields = {
            "image": "image_key",
            "audio": "file_key",
            "video": "file_key",
            "file": "file_key",
            "sticker": "file_key",
        }
        key_name = key_fields.get(msg_type)
        if not key_name:
            return []
        key_val = payload.get(key_name)
        if not isinstance(key_val, str) or not key_val.strip():
            return []
        return [
            {
                "key": key_val.strip(),
                "uri": self._build_media_uri("feishu", msg_type, key_val.strip()),
            }
        ]

    async def send_voice_message(self, chat_id: str, audio_path: Path) -> None:
        """Send voice message to Feishu, best-effort helper for TTS integration."""
        if not audio_path.exists():
            logger.error(f"Feishu: audio file not found: {audio_path}")
            return
        if not self._client:
            logger.error("Feishu client is not initialized")
            return
        token = await self._get_tenant_access_token()
        if not token:
            return
        import httpx

        upload_url = "https://open.feishu.cn/open-apis/im/v1/files"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                with audio_path.open("rb") as f:
                    files = {
                        "file_type": (None, "opus"),
                        "file_name": (None, audio_path.name),
                        "file": (audio_path.name, f, "audio/mpeg"),
                    }
                    upload_resp = await client.post(
                        upload_url, files=files, headers={"Authorization": f"Bearer {token}"}
                    )
                    upload_data = upload_resp.json()
                file_key = str(upload_data.get("data", {}).get("file_key", ""))
                if not file_key:
                    logger.error(f"Feishu audio upload failed: {upload_data}")
                    return
                send_url = "https://open.feishu.cn/open-apis/im/v1/messages"
                payload = {
                    "receive_id": chat_id,
                    "msg_type": "audio",
                    "content": json.dumps({"file_key": file_key}),
                }
                await client.post(
                    send_url,
                    params={"receive_id_type": "chat_id"},
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
        except Exception as exc:
            logger.error(f"Feishu voice message error: {exc}")

    async def _maybe_transcribe_media(self, media_uri: str, msg_type: str) -> str:
        if msg_type not in {"audio", "video"} or not self.groq_api_key:
            return ""
        local_path = self._local_path_from_media_uri(media_uri)
        if local_path is None or not is_supported_audio_file(local_path):
            return ""
        try:
            transcriber = GroqTranscriptionProvider(api_key=self.groq_api_key)
            return (await transcriber.transcribe(local_path)).strip()
        except Exception as e:
            logger.warning(f"Feishu transcription failed: {e}")
            return ""

    def _local_path_from_media_uri(self, media_uri: str) -> Path | None:
        if not media_uri.startswith("media://local/"):
            return None
        name = media_uri.rsplit("/", 1)[-1]
        p = self.media_root / name
        return p if p.exists() else None

"""DingTalk bot channel."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from zen_claw.bus.events import OutboundMessage
from zen_claw.bus.queue import MessageBus
from zen_claw.channels.base import BaseChannel
from zen_claw.config.schema import DingTalkConfig
from zen_claw.providers.transcription import GroqTranscriptionProvider, is_supported_audio_file

try:
    from dingtalk_stream import CallbackMessage, DingTalkStreamClient
    DINGTALK_STREAM_AVAILABLE = True
except ImportError:
    DINGTALK_STREAM_AVAILABLE = False


class DingTalkChannel(BaseChannel):
    name = "dingtalk"

    def __init__(self, config: DingTalkConfig, bus: MessageBus, media_root: Path | None = None) -> None:
        super().__init__(config, bus, media_root=media_root)
        self.config = config
        self.groq_api_key = os.environ.get("GROQ_API_KEY", "")
        self._stop_event = asyncio.Event()
        self._client: DingTalkStreamClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._executor_future: asyncio.Future | None = None

    async def start(self) -> None:
        self._running = True
        self._loop = asyncio.get_running_loop()

        if self.config.app_key and self.config.app_secret and DINGTALK_STREAM_AVAILABLE:
            # Prefer Stream mode if AppKey and AppSecret are provided
            logger.info("Starting DingTalk in Stream (WebSocket) mode")
            self._client = DingTalkStreamClient(self.config.app_key, self.config.app_secret)
            self._client.register_callback_listener('/v1.0/im/bot/messages/get', self._on_stream_message)

            # Start stream client in background (it uses its own thread/loop)
            def _start_client():
                try:
                    self._client.start()
                except Exception as e:
                    logger.error(f"DingTalk stream client error: {e}")

            # Start client using asyncio thread pool.
            # Store the Future so it is not garbage-collected and add a
            # done-callback to detect thread exit (crash or clean shutdown).
            def _on_executor_done(fut: asyncio.Future) -> None:
                exc = fut.exception()
                if exc:
                    logger.error(
                        "DingTalk stream thread raised unhandled exception: {}", exc
                    )
                else:
                    logger.warning("DingTalk stream client thread exited")
                self._running = False

            self._executor_future = self._loop.run_in_executor(None, _start_client)
            self._executor_future.add_done_callback(_on_executor_done)
        else:
            if not self.config.webhook_url:
                logger.warning("DingTalk: Neither Stream (AppKey/Secret) nor Webhook URL configured, or dingtalk-stream not installed. Channel will run but not receive events directly unless webhook is routed by API Gateway.")
            else:
                logger.info(f"Starting DingTalk in Webhook mode, webhook URL: {self.config.webhook_url[:20]}...")

        await self._stop_event.wait()
        self._running = False

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()

    async def send(self, msg: OutboundMessage) -> None:
        if not self.config.webhook_url:
            logger.warning("DingTalk: webhook_url is required to send messages, even in Stream mode.")
            return

        content_str = msg.content or ""

        # Super basic media appending since DingTalk's webhook rich media differs heavily by msgtype
        if msg.media:
            for uri in msg.media:
                if uri.startswith("media://local/"):
                    content_str += f"\n[Media attached: {uri}]"

        payload = {"msgtype": "markdown", "markdown": {"title": "Agent Reply", "text": content_str}}
        await self._post_to_dingtalk(payload)

    def _on_stream_message(self, message: CallbackMessage) -> None:
        """Handler for DingTalk Stream incoming messages."""
        if not self._loop or not self._loop.is_running():
            return

        try:
            body = message.data
            asyncio.run_coroutine_threadsafe(self._process_stream_message(body), self._loop)
        except Exception as e:
            logger.error(f"Error handling DingTalk stream message: {e}")

    @staticmethod
    def compute_outgoing_sign(secret: str, timestamp_ms: int) -> str:
        plain = f"{timestamp_ms}\n{secret}"
        sig = hmac.new(secret.encode("utf-8"), plain.encode("utf-8"), digestmod=hashlib.sha256).digest()
        return urllib.parse.quote_plus(base64.b64encode(sig).decode("utf-8"))

    @staticmethod
    def verify_incoming_sign(timestamp_str: str, sign_header: str, secret: str, max_age_ms: int = 3600_000) -> bool:
        if not secret:
            return True
        try:
            ts_ms = int(timestamp_str)
        except (TypeError, ValueError):
            return False
        if abs(int(time.time() * 1000) - ts_ms) > max_age_ms:
            return False
        plain = f"{ts_ms}\n{secret}"
        expected = base64.b64encode(
            hmac.new(secret.encode("utf-8"), plain.encode("utf-8"), digestmod=hashlib.sha256).digest()
        ).decode("utf-8")
        return hmac.compare_digest(sign_header, expected)

    async def handle_webhook(self, body: dict[str, Any]) -> dict[str, Any]:
        """Backward compatible webhook handler for API Gateway."""
        if self.config.secret:
            ts = str(body.get("timestamp", ""))
            sign = str(body.get("sign", ""))
            if not self.verify_incoming_sign(ts, sign, self.config.secret):
                return {"success": False, "reason": "invalid_signature"}
        msg_type = str(body.get("msgtype", "")).lower()
        sender = str(body.get("senderStaffId") or body.get("senderId") or "unknown")
        chat_id = str(body.get("conversationId") or sender)
        if msg_type == "text":
            content = str((body.get("text") or {}).get("content", "")).strip()
        elif msg_type == "picture":
            content = "[image]"
            pic_url = body.get("content", {}).get("downloadCode") or body.get("content", {}).get("picUrl") or ""
            if pic_url:
                content += f"\n[media_ref: {pic_url}]"
        elif msg_type == "audio":
            content = "[audio]"
            audio_url = body.get("content", {}).get("downloadCode") or ""
            if audio_url:
                content += f"\n[media_ref: {audio_url}]"
                local_path = await self._download_media_ref(str(audio_url), suffix=".amr")
                transcription = await self._transcribe_audio(local_path)
                if transcription:
                    content += f'\n[Voice]: "{transcription}"'
        else:
            content = f"[{msg_type} message]"

        if content:
            await self._handle_message(sender_id=sender, chat_id=chat_id, content=content, metadata={"dingtalk_raw": body, "mode": "webhook"})
        return {"success": True}

    async def _post_to_dingtalk(self, payload: dict[str, Any]) -> None:
        url = self.config.webhook_url
        if self.config.secret:
            ts = int(time.time() * 1000)
            sign = self.compute_outgoing_sign(self.config.secret, ts)
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}timestamp={ts}&sign={sign}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(url, json=payload, headers={"Content-Type": "application/json"})
        except Exception as exc:
            logger.error(f"DingTalk send failed: {exc}")

    async def _download_media_ref(self, media_ref: str, suffix: str = ".bin") -> Path | None:
        if not media_ref or not media_ref.startswith("http"):
            return None
        self.media_root.mkdir(parents=True, exist_ok=True)
        out = self.media_root / f"dingtalk_{int(time.time() * 1000)}{suffix}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(media_ref)
                resp.raise_for_status()
                out.write_bytes(resp.content)
            return out
        except Exception as e:
            logger.warning(f"DingTalk media download failed: {e}")
            return None

    async def _transcribe_audio(self, path: Path | None) -> str:
        if path is None or not self.groq_api_key:
            return ""
        if not is_supported_audio_file(path):
            return ""
        try:
            transcriber = GroqTranscriptionProvider(api_key=self.groq_api_key)
            return (await transcriber.transcribe(path)).strip()
        except Exception as e:
            logger.warning(f"DingTalk transcription failed: {e}")
            return ""

    async def _process_stream_message(self, body: dict[str, Any]) -> None:
        msg_type = str(body.get("msgtype", "")).lower()
        sender = str(body.get("senderStaffId") or body.get("senderId") or "unknown")
        chat_id = str(body.get("conversationId") or sender)
        if msg_type == "text":
            content = str((body.get("text") or {}).get("content", "")).strip()
        elif msg_type == "audio":
            content = "[audio message]"
            media_ref = str((body.get("content") or {}).get("downloadCode") or "").strip()
            if media_ref:
                content += f"\n[media_ref: {media_ref}]"
            local_path = await self._download_media_ref(media_ref, suffix=".amr")
            transcription = await self._transcribe_audio(local_path)
            if transcription:
                content += f'\n[Voice]: "{transcription}"'
        else:
            content = f"[{msg_type} message]"
        if not content:
            return
        await self._handle_message(
            sender_id=sender,
            chat_id=chat_id,
            content=content,
            metadata={"dingtalk_raw": body, "mode": "stream"},
        )

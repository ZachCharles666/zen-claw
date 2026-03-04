"""Slack channel implementation with Socket Mode and HTTP callback support."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from zen_claw.bus.events import OutboundMessage
from zen_claw.bus.queue import MessageBus
from zen_claw.channels.base import BaseChannel
from zen_claw.config.schema import SlackConfig


class SlackChannel(BaseChannel):
    """Slack channel with optional dependency on `slack_sdk` / `slack_bolt`."""

    name = "slack"

    def __init__(self, config: SlackConfig, bus: MessageBus, media_root=None):
        super().__init__(config, bus, media_root=media_root)
        self.config: SlackConfig = config
        self._client = None
        self._socket_handler = None
        self._socket_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
        if not self.config.bot_token:
            logger.warning("Slack bot_token not configured; channel started in passive mode")
            return
        try:
            from slack_sdk.web.async_client import AsyncWebClient  # type: ignore
        except Exception:
            logger.warning("slack_sdk is not installed; Slack channel running in passive mode")
            return
        self._client = AsyncWebClient(token=self.config.bot_token)
        if self.config.socket_mode and self.config.app_token:
            await self._start_socket_mode()

    async def stop(self) -> None:
        self._running = False
        if self._socket_task:
            self._socket_task.cancel()
            self._socket_task = None
        self._socket_handler = None
        self._client = None

    async def send(self, msg: OutboundMessage) -> None:
        if self._client is None:
            logger.warning("Slack send skipped: client unavailable")
            return
        try:
            blocks = self._resolve_blocks(msg)
            payload: dict[str, Any] = {"channel": msg.chat_id, "text": msg.content}
            if blocks:
                payload["blocks"] = blocks
            if msg.reply_to:
                payload["thread_ts"] = msg.reply_to
            await self._client.chat_postMessage(**payload)
            await self._upload_files(msg)
        except Exception as e:
            logger.warning(f"Slack send failed: {e}")

    async def handle_http_event(self, body: bytes, headers: dict[str, str]) -> dict[str, Any]:
        """Handle Slack Events API callback payload."""
        if not self._verify_signature(body=body, headers=headers):
            return {"ok": False, "reason": "invalid_signature"}
        import json

        payload = json.loads(body.decode("utf-8"))
        if payload.get("type") == "url_verification":
            return {"ok": True, "challenge": payload.get("challenge", "")}
        event = payload.get("event")
        if isinstance(event, dict):
            await self.ingest_event(event)
        return {"ok": True}

    async def ingest_event(self, event: dict[str, Any]) -> None:
        """Ingest an inbound Slack event from socket mode or HTTP callback."""
        if str(event.get("type") or "") != "message":
            return
        if event.get("subtype") in {"bot_message", "message_changed", "message_deleted"}:
            return
        sender_id = str(event.get("user") or "")
        chat_id = str(event.get("channel") or "")
        if not sender_id or not chat_id:
            return
        if not self.is_allowed(sender_id):
            return
        text = str(event.get("text") or "").strip()
        content_parts: list[str] = [text] if text else []
        media_paths: list[str] = []
        media_refs: list[str] = []
        for f in event.get("files") or []:
            ref = await self._resolve_inbound_file(f)
            if ref:
                media_paths.append(ref["path"])
                media_refs.append(ref["uri"])
                content_parts.append(f"[media_ref: {ref['uri']}]")
        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content="\n".join(content_parts) if content_parts else "[empty message]",
            media=media_paths,
            metadata={
                "thread_ts": event.get("thread_ts") or event.get("ts"),
                "message_ts": event.get("ts"),
                "media_refs": media_refs,
            },
        )

    async def _start_socket_mode(self) -> None:
        try:
            from slack_bolt.adapter.socket_mode.aiohttp import (
                AsyncSocketModeHandler,  # type: ignore
            )
            from slack_bolt.async_app import AsyncApp  # type: ignore
        except Exception:
            logger.warning("slack_bolt is not installed; Slack socket mode disabled")
            return
        app = AsyncApp(token=self.config.bot_token)

        @app.event("message")
        async def _on_message_events(event, say):  # type: ignore[no-untyped-def]
            await self.ingest_event(event)

        self._socket_handler = AsyncSocketModeHandler(app, self.config.app_token)
        self._socket_task = asyncio.create_task(self._socket_handler.start_async())

    def _resolve_blocks(self, msg: OutboundMessage) -> list[dict[str, Any]]:
        blocks = (msg.metadata or {}).get("blocks")
        if isinstance(blocks, list):
            return [b for b in blocks if isinstance(b, dict)]
        return [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": msg.content or ""},
            }
        ]

    async def _upload_files(self, msg: OutboundMessage) -> None:
        if self._client is None or not msg.media:
            return
        for media in msg.media[:4]:
            if not isinstance(media, str) or not media.strip():
                continue
            p = Path(media)
            if not p.exists() or not p.is_file():
                continue
            try:
                await self._client.files_upload_v2(
                    channel=msg.chat_id,
                    title=p.name,
                    file=str(p),
                    thread_ts=msg.reply_to or None,
                )
            except Exception as e:
                logger.warning(f"Slack file upload failed for {p}: {e}")

    async def _resolve_inbound_file(self, file_obj: dict[str, Any]) -> dict[str, str] | None:
        file_id = str(file_obj.get("id") or "").strip()
        if not file_id:
            return None
        source_url = str(file_obj.get("url_private_download") or file_obj.get("url_private") or "").strip()
        media_type = self._slack_file_type(file_obj)
        uri = self._build_media_uri("slack", media_type, file_id)
        if not source_url or not self.config.bot_token:
            return {"path": uri, "uri": uri}
        try:
            self.media_root.mkdir(parents=True, exist_ok=True)
            ext = Path(str(file_obj.get("name") or file_id)).suffix or ""
            out_path = self.media_root / f"slack_{file_id}{ext}"
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    source_url,
                    headers={"Authorization": f"Bearer {self.config.bot_token}"},
                )
                resp.raise_for_status()
                out_path.write_bytes(resp.content)
            return {"path": str(out_path), "uri": uri}
        except Exception as e:
            logger.warning(f"Slack inbound file download failed: {e}")
            return {"path": uri, "uri": uri}

    @staticmethod
    def _slack_file_type(file_obj: dict[str, Any]) -> str:
        ctype = str(file_obj.get("mimetype") or "").lower()
        if ctype.startswith("image/"):
            return "image"
        if ctype.startswith("audio/"):
            return "audio"
        if ctype.startswith("video/"):
            return "video"
        return "file"

    def _verify_signature(self, body: bytes, headers: dict[str, str]) -> bool:
        secret = str(self.config.signing_secret or "").strip()
        if not secret:
            return True
        ts = str(headers.get("x-slack-request-timestamp") or "").strip()
        sig = str(headers.get("x-slack-signature") or "").strip()
        if not ts or not sig:
            return False
        try:
            now = int(time.time())
            if abs(now - int(ts)) > 60 * 5:
                return False
        except Exception:
            return False
        base = f"v0:{ts}:".encode("utf-8") + body
        expected = "v0=" + hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)

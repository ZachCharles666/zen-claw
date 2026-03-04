"""WeChat MP channel."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import struct
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from zen_claw.bus.events import OutboundMessage
from zen_claw.bus.queue import MessageBus
from zen_claw.channels.base import BaseChannel
from zen_claw.config.schema import WechatMPConfig
from zen_claw.providers.transcription import GroqTranscriptionProvider, is_supported_audio_file

try:
    from Crypto.Cipher import AES

    _HAS_CRYPTO = True
except Exception:
    _HAS_CRYPTO = False


class WechatMPChannel(BaseChannel):
    name = "wechat_mp"

    def __init__(self, config: WechatMPConfig, bus: MessageBus, media_root: Path | None = None) -> None:
        super().__init__(config, bus, media_root=media_root)
        self.config = config
        self.groq_api_key = os.environ.get("GROQ_API_KEY", "")
        self._access_token_cache: dict[str, Any] = {}  # instance-level, not shared across instances
        self._stop_event = asyncio.Event()
        self._token_lock = asyncio.Lock()
        self._aes_key = None
        if config.encoding_aes_key:
            try:
                self._aes_key = base64.b64decode(config.encoding_aes_key + "=")
            except Exception:
                self._aes_key = None

    async def start(self) -> None:
        self._running = True
        await self._stop_event.wait()
        self._running = False

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()

    async def send(self, msg: OutboundMessage) -> None:
        # WeChat MP uses async customer service API; keep no-op fallback if metadata lacks to_user.
        to_user = str((msg.metadata or {}).get("wechat_from_user", ""))
        if not to_user:
            return
        token = await self._get_access_token()
        if not token:
            return

        content_str = msg.content or ""

        # Process outgoing media uploads if present
        if msg.media:
            for uri in msg.media:
                if uri.startswith("media://local/"):
                    local_path = self.media_root / uri.split("/")[-1]
                    if local_path.exists():
                        media_id = await self.upload_media(local_path, "image", token) # Hardcode image for demo
                        if media_id:
                            # Send image message directly using a separate customer service API call
                            await self._send_customer_service_image(to_user=to_user, media_id=media_id, access_token=token)
                        else:
                            content_str += f"\n[Attached Media {uri} failed to upload]"

        # Send text if it's not empty after stripping
        if content_str.strip():
            await self._send_customer_service_message(to_user=to_user, text=content_str, access_token=token)

    @staticmethod
    def verify_signature(token: str, timestamp: str, nonce: str, signature: str) -> bool:
        joined = "".join(sorted([token, timestamp, nonce]))
        expected = hashlib.sha1(joined.encode("utf-8")).hexdigest()
        return expected == signature

    @staticmethod
    def parse_xml_message(xml_str: str) -> dict[str, str]:
        out: dict[str, str] = {}
        try:
            root = ET.fromstring(xml_str)
        except ET.ParseError:
            return out
        for child in root:
            out[child.tag] = child.text or ""
        return out

    def decrypt_message(self, encrypted_b64: str) -> str | None:
        if not _HAS_CRYPTO or not self._aes_key:
            return None
        try:
            encrypted = base64.b64decode(encrypted_b64)
            iv = self._aes_key[:16]
            cipher = AES.new(self._aes_key, AES.MODE_CBC, iv)
            decrypted = cipher.decrypt(encrypted)
            pad_len = decrypted[-1]
            decrypted = decrypted[:-pad_len]
            msg_len = struct.unpack(">I", decrypted[16:20])[0]
            msg = decrypted[20 : 20 + msg_len]
            return msg.decode("utf-8")
        except Exception:
            return None

    async def handle_webhook(
        self,
        raw_xml: str,
        signature: str,
        timestamp: str,
        nonce: str,
        msg_signature: str | None = None,
        encrypted: bool = True,
    ) -> str:
        if not self.verify_signature(self.config.token, timestamp, nonce, signature):
            return "fail"
        xml_str = raw_xml
        if encrypted:
            outer = self.parse_xml_message(raw_xml)
            payload = outer.get("Encrypt", "")
            if not payload:
                return "fail"
            xml_str = self.decrypt_message(payload) or ""
            if not xml_str:
                return "fail"
        msg = self.parse_xml_message(xml_str)
        sender = msg.get("FromUserName", "")
        msg_type = msg.get("MsgType", "").lower()
        if not sender:
            return "success"
        content = ""
        media_refs = []

        token = await self._get_access_token()

        if msg_type == "text":
            content = msg.get("Content", "").strip()
        elif msg_type == "image":
            media_id = msg.get("MediaId", "")
            content = f"[图片 MediaId={media_id}]"
            if media_id and token:
                local_uri = await self.download_media(media_id, "image", token)
                if local_uri:
                    media_refs.append(local_uri)
                    content += f"\n[media_ref: {local_uri}]"
        elif msg_type == "voice":
            media_id = msg.get("MediaId", "")
            content = msg.get("Recognition", "") or "[语音消息]"
            if media_id and token:
                local_uri = await self.download_media(media_id, "voice", token)
                if local_uri:
                    media_refs.append(local_uri)
                    content += f"\n[media_ref: {local_uri}]"
                    if not msg.get("Recognition", ""):
                        local_path = self._local_path_from_media_uri(local_uri)
                        transcription = await self._transcribe_audio(local_path)
                        if transcription:
                            content += f'\n[Voice]: "{transcription}"'

        if content:
            await self._handle_message(
                sender_id=sender,
                chat_id=sender,
                content=content,
                media=media_refs,
                metadata={"wechat_from_user": sender}
            )
        return "success"

    async def _get_access_token(self) -> str:
        key = self.config.app_id

        # Fast path lock-free check
        cached = self._access_token_cache.get(key)
        if cached and time.time() < cached.get("expires_at", 0):
            return str(cached.get("token", ""))

        async with self._token_lock:
            # Recheck after acquiring lock to handle concurrent refresh
            cached = self._access_token_cache.get(key)
            if cached and time.time() < cached.get("expires_at", 0):
                return str(cached.get("token", ""))

            url = f"https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid={self.config.app_id}&secret={self.config.app_secret}"
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    data = (await client.get(url)).json()
                token = str(data.get("access_token", ""))
                expires = int(data.get("expires_in", 7200))
                self._access_token_cache[key] = {"token": token, "expires_at": time.time() + max(0, expires - 200)}
                return token
            except Exception as exc:
                logger.error(f"Wechat token fetch failed: {exc}")
                return ""

    async def _send_customer_service_message(self, to_user: str, text: str, access_token: str) -> None:
        url = f"https://api.weixin.qq.com/cgi-bin/message/custom/send?access_token={access_token}"
        payload = {"touser": to_user, "msgtype": "text", "text": {"content": text}}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(url, json=payload)
        except Exception as exc:
            logger.error(f"Wechat send failed: {exc}")

    async def _send_customer_service_image(self, to_user: str, media_id: str, access_token: str) -> None:
        url = f"https://api.weixin.qq.com/cgi-bin/message/custom/send?access_token={access_token}"
        payload = {"touser": to_user, "msgtype": "image", "image": {"media_id": media_id}}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(url, json=payload)
        except Exception as exc:
            logger.error(f"Wechat send image failed: {exc}")

    async def download_media(self, media_id: str, media_type: str, access_token: str) -> str | None:
        """Download temporary media from WeChat."""
        url = f"https://api.weixin.qq.com/cgi-bin/media/get?access_token={access_token}&media_id={media_id}"
        ext = ".amr" if media_type == "voice" else ".jpg"
        file_name = f"wechat_{media_id}{ext}"
        self.media_root.mkdir(parents=True, exist_ok=True)
        out_path = self.media_root / file_name

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        return None

                    # If it's JSON encoded error
                    content_type = response.headers.get("Content-Type", "")
                    if "text/plain" in content_type or "application/json" in content_type:
                        err_data = await response.aread()
                        logger.warning(f"Wechat media download error: {err_data.decode('utf-8')}")
                        return None

                    with out_path.open("wb") as f:
                        async for chunk in response.aiter_bytes():
                            f.write(chunk)
            return f"media://local/wechat/{file_name}"
        except Exception as exc:
            logger.warning(f"Error downloading WeChat media {media_id}: {exc}")
            return None

    async def upload_media(self, file_path: Path, media_type: str, access_token: str) -> str | None:
        """Upload temporary media to WeChat and return MediaId."""
        url = f"https://api.weixin.qq.com/cgi-bin/media/upload?access_token={access_token}&type={media_type}"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                with file_path.open("rb") as f:
                    files = {"media": (file_path.name, f)}
                    response = await client.post(url, files=files)

                data = response.json()
                if "media_id" in data:
                    return data["media_id"]
                else:
                    logger.warning(f"Wechat media upload failed: {data}")
                    return None
        except Exception as exc:
            logger.warning(f"Error uploading WeChat media {file_path}: {exc}")
            return None

    def _local_path_from_media_uri(self, media_uri: str) -> Path | None:
        if not media_uri.startswith("media://local/wechat/"):
            return None
        p = self.media_root / media_uri.rsplit("/", 1)[-1]
        return p if p.exists() else None

    async def _transcribe_audio(self, path: Path | None) -> str:
        if path is None or not self.groq_api_key:
            return ""
        if not is_supported_audio_file(path):
            return ""
        try:
            transcriber = GroqTranscriptionProvider(api_key=self.groq_api_key)
            return (await transcriber.transcribe(path)).strip()
        except Exception as e:
            logger.warning(f"WeChat transcription failed: {e}")
            return ""

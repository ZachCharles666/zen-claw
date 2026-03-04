"""Enterprise WeCom channel."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import struct
import time
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from zen_claw.bus.events import OutboundMessage
from zen_claw.bus.queue import MessageBus
from zen_claw.channels.base import BaseChannel
from zen_claw.channels.wechat_mp import WechatMPChannel
from zen_claw.config.schema import WeComConfig
from zen_claw.providers.transcription import GroqTranscriptionProvider, is_supported_audio_file

try:
    from Crypto.Cipher import AES

    _HAS_CRYPTO = True
except Exception:
    _HAS_CRYPTO = False


class WeComChannel(BaseChannel):
    name = "wecom"
    _ACCESS_TOKEN_CACHE: dict[str, Any] = {}

    def __init__(self, config: WeComConfig, bus: MessageBus, media_root: Path | None = None) -> None:
        super().__init__(config, bus, media_root=media_root)
        self.config = config
        self.groq_api_key = os.environ.get("GROQ_API_KEY", "")
        self._stop_event = asyncio.Event()
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
        to_user = str((msg.metadata or {}).get("wecom_from_user", ""))
        if not to_user:
            return
        token = await self._get_access_token()
        if not token:
            return
        await self._send_app_message(to_user=to_user, text=msg.content or "", access_token=token)

    def verify_signature(self, timestamp: str, nonce: str, msg_signature: str, echostr: str = "") -> bool:
        joined = "".join(sorted([self.config.token, timestamp, nonce, echostr]))
        expected = hashlib.sha1(joined.encode("utf-8")).hexdigest()
        return expected == msg_signature

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
            return decrypted[20 : 20 + msg_len].decode("utf-8")
        except Exception:
            return None

    async def handle_webhook(self, raw_xml: str, timestamp: str, nonce: str, msg_signature: str) -> str:
        outer = WechatMPChannel.parse_xml_message(raw_xml)
        encrypted = outer.get("Encrypt", "")
        if not self.verify_signature(timestamp, nonce, msg_signature, encrypted):
            return "fail"
        plain = self.decrypt_message(encrypted)
        if not plain:
            return "fail"
        msg = WechatMPChannel.parse_xml_message(plain)
        sender = msg.get("FromUserName", "")
        msg_type = msg.get("MsgType", "").lower()
        if not sender:
            return "success"
        content = ""
        media_refs: list[str] = []
        if msg_type == "text":
            content = msg.get("Content", "").strip()
        elif msg_type == "image":
            content = f"[图片 MediaId={msg.get('MediaId', '')}]"
        elif msg_type == "voice":
            media_id = msg.get("MediaId", "")
            content = msg.get("Recognition", "") or "[语音消息]"
            token = await self._get_access_token()
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
                metadata={"wecom_from_user": sender},
            )
        return "success"

    async def _get_access_token(self) -> str:
        key = self.config.corp_id
        cached = self._ACCESS_TOKEN_CACHE.get(key)
        if cached and time.time() < cached.get("expires_at", 0):
            return str(cached.get("token", ""))
        url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={self.config.corp_id}&corpsecret={self.config.corp_secret}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                data = (await client.get(url)).json()
            token = str(data.get("access_token", ""))
            expires = int(data.get("expires_in", 7200))
            self._ACCESS_TOKEN_CACHE[key] = {"token": token, "expires_at": time.time() + max(0, expires - 200)}
            return token
        except Exception as exc:
            logger.error(f"WeCom token fetch failed: {exc}")
            return ""

    async def _send_app_message(self, to_user: str, text: str, access_token: str) -> None:
        url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={access_token}"
        payload = {"touser": to_user, "msgtype": "text", "agentid": self.config.agent_id, "text": {"content": text}}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(url, json=payload)
        except Exception as exc:
            logger.error(f"WeCom send failed: {exc}")

    async def download_media(self, media_id: str, media_type: str, access_token: str) -> str | None:
        url = f"https://qyapi.weixin.qq.com/cgi-bin/media/get?access_token={access_token}&media_id={media_id}"
        ext = ".amr" if media_type == "voice" else ".jpg"
        file_name = f"wecom_{media_id}{ext}"
        self.media_root.mkdir(parents=True, exist_ok=True)
        out_path = self.media_root / file_name
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        return None
                    with out_path.open("wb") as f:
                        async for chunk in response.aiter_bytes():
                            f.write(chunk)
            return f"media://local/wecom/{file_name}"
        except Exception as e:
            logger.warning(f"WeCom media download failed: {e}")
            return None

    def _local_path_from_media_uri(self, media_uri: str) -> Path | None:
        if not media_uri.startswith("media://local/wecom/"):
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
            logger.warning(f"WeCom transcription failed: {e}")
            return ""

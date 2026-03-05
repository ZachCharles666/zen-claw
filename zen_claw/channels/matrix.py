"""Matrix channel implementation (REST sync/send baseline)."""

from __future__ import annotations

import asyncio
import mimetypes
from urllib.parse import quote

import httpx
from loguru import logger

from zen_claw.bus.events import OutboundMessage
from zen_claw.bus.queue import MessageBus
from zen_claw.channels.base import BaseChannel
from zen_claw.config.schema import MatrixConfig


class MatrixChannel(BaseChannel):
    """Matrix channel with lightweight client-server REST integration."""

    name = "matrix"

    def __init__(self, config: MatrixConfig, bus: MessageBus, media_root=None):
        super().__init__(config, bus, media_root=media_root)
        self.config: MatrixConfig = config
        self._http: httpx.AsyncClient | None = None
        self._sync_task: asyncio.Task | None = None
        self._since: str = ""
        self._e2ee_available = False
        self._nio_client = None

    async def start(self) -> None:
        self._running = True
        self._http = httpx.AsyncClient(timeout=30.0)
        await self._ensure_access_token()
        self._e2ee_available = self._check_e2ee_runtime()
        if self.config.e2ee_require and self.config.e2ee_enabled and not self._e2ee_available:
            logger.error("Matrix E2E required but runtime dependencies are unavailable")
            self._running = False
            return
        await self._maybe_init_nio_e2e()
        if self.config.access_token:
            self._sync_task = asyncio.create_task(self._sync_loop())
        logger.info("Matrix channel started for homeserver={}", self.config.homeserver)

    async def stop(self) -> None:
        self._running = False
        if self._sync_task:
            self._sync_task.cancel()
            self._sync_task = None
        if self._nio_client and hasattr(self._nio_client, "close"):
            try:
                await self._nio_client.close()
            except Exception:
                pass
            self._nio_client = None
        if self._http:
            await self._http.aclose()
            self._http = None

    async def send(self, msg: OutboundMessage) -> None:
        if not self.config.access_token:
            logger.warning("Matrix send skipped: access_token not configured")
            return
        try:
            if await self._send_via_nio_if_available(msg):
                return
            txn_id = f"nano-{int(asyncio.get_running_loop().time() * 1000)}"
            room_id = quote(msg.chat_id, safe="")
            txn = quote(txn_id, safe="")
            encrypted_payload = self._build_encrypted_payload(msg)
            if encrypted_payload:
                await self._matrix_put(
                    f"/_matrix/client/v3/rooms/{room_id}/send/m.room.encrypted/{txn}",
                    encrypted_payload,
                )
            else:
                payload = self._build_text_payload(msg)
                await self._matrix_put(
                    f"/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn}",
                    payload,
                )
            await self._send_media_events(msg)
        except Exception as exc:
            logger.warning("Matrix send failed: {}", exc)

    async def _sync_loop(self) -> None:
        while self._running and self.config.access_token:
            try:
                data = await self._matrix_get(
                    "/_matrix/client/v3/sync", params={"timeout": "25000", "since": self._since}
                )
                await self._process_sync_response(data)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Matrix sync tick failed: {}", exc)
            await asyncio.sleep(1.0)

    async def _process_sync_response(self, data: dict) -> None:
        if not isinstance(data, dict):
            return
        self._since = str(data.get("next_batch") or self._since)
        rooms = data.get("rooms") or {}
        join = rooms.get("join") if isinstance(rooms, dict) else {}
        if not isinstance(join, dict):
            return
        for room_id, room_data in join.items():
            timeline = (room_data or {}).get("timeline") if isinstance(room_data, dict) else {}
            events = timeline.get("events") if isinstance(timeline, dict) else []
            if not isinstance(events, list):
                continue
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                ev_type = str(ev.get("type") or "")
                if ev_type not in {"m.room.message", "m.room.encrypted"}:
                    continue
                sender = str(ev.get("sender") or "")
                if sender and self.config.user_id and sender == self.config.user_id:
                    continue
                content = ev.get("content") or {}
                body = ""
                extra_meta: dict[str, str | bool] = {}
                if isinstance(content, dict):
                    if ev_type == "m.room.message":
                        msgtype = str(content.get("msgtype") or "m.text")
                        body = str(content.get("body") or "").strip()
                        if msgtype in {"m.image", "m.file", "m.audio", "m.video"}:
                            body = body or f"[{msgtype}]"
                            media_refs: list[str] = []
                            downloaded: list[str] = []
                            mxc_url = str(content.get("url") or "").strip()
                            if mxc_url:
                                media_refs.append(self._mxc_to_media_ref(mxc_url, msgtype))
                                if self.config.media_download:
                                    local = await self._download_mxc(mxc_url)
                                    if local:
                                        downloaded.append(local)
                            extra_meta = {
                                "msgtype": msgtype,
                                "media_refs": media_refs,
                            }
                            await self._handle_message(
                                sender_id=sender,
                                chat_id=str(room_id),
                                content=body,
                                media=downloaded,
                                metadata={"event_id": str(ev.get("event_id") or ""), **extra_meta},
                            )
                            continue
                    else:
                        body = "[encrypted message]"
                        extra_meta = {
                            "encrypted": True,
                            "algorithm": str(content.get("algorithm") or ""),
                            "device_id": str(content.get("device_id") or ""),
                            "sender_key": str(content.get("sender_key") or ""),
                        }
                if not sender or not body:
                    continue
                await self._handle_message(
                    sender_id=sender,
                    chat_id=str(room_id),
                    content=body,
                    metadata={"event_id": str(ev.get("event_id") or ""), **extra_meta},
                )

    async def _matrix_get(self, path: str, params: dict[str, str] | None = None) -> dict:
        if not self._http:
            self._http = httpx.AsyncClient(timeout=30.0)
        url = f"{self.config.homeserver.rstrip('/')}{path}"
        resp = await self._http.get(url, params=params or {}, headers=self._headers())
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    async def _matrix_put(self, path: str, payload: dict) -> dict:
        if not self._http:
            self._http = httpx.AsyncClient(timeout=30.0)
        url = f"{self.config.homeserver.rstrip('/')}{path}"
        resp = await self._http.put(url, json=payload, headers=self._headers())
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    async def _matrix_post(self, path: str, payload: dict) -> dict:
        if not self._http:
            self._http = httpx.AsyncClient(timeout=30.0)
        url = f"{self.config.homeserver.rstrip('/')}{path}"
        resp = await self._http.post(url, json=payload, headers=self._headers(optional=True))
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    async def _matrix_post_raw(self, path: str, content: bytes, content_type: str) -> dict:
        if not self._http:
            self._http = httpx.AsyncClient(timeout=30.0)
        url = f"{self.config.homeserver.rstrip('/')}{path}"
        headers = self._headers()
        headers["Content-Type"] = content_type
        resp = await self._http.post(url, content=content, headers=headers)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    async def _ensure_access_token(self) -> None:
        if self.config.access_token.strip():
            return
        username = self.config.username.strip()
        password = self.config.password
        if not username or not password:
            return

        if self.config.auto_register:
            try:
                await self._register(username, password)
                return
            except Exception as exc:
                logger.debug("Matrix auto-register failed, fallback to login: {}", exc)
        if self.config.auto_login:
            try:
                await self._login(username, password)
            except Exception as exc:
                logger.warning("Matrix auto-login failed: {}", exc)

    async def _register(self, username: str, password: str) -> None:
        payload = {
            "username": username,
            "password": password,
            "auth": {"type": "m.login.dummy"},
            "device_id": self.config.device_id or "",
            "initial_device_display_name": self.config.device_name,
        }
        resp = await self._matrix_post("/_matrix/client/v3/register", payload)
        self._hydrate_auth_from_response(resp)

    async def _login(self, username: str, password: str) -> None:
        payload = {
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": username},
            "password": password,
            "device_id": self.config.device_id or "",
            "initial_device_display_name": self.config.device_name,
        }
        resp = await self._matrix_post("/_matrix/client/v3/login", payload)
        self._hydrate_auth_from_response(resp)

    def _hydrate_auth_from_response(self, data: dict) -> None:
        token = str((data or {}).get("access_token") or "").strip()
        if token:
            self.config.access_token = token
        user_id = str((data or {}).get("user_id") or "").strip()
        if user_id:
            self.config.user_id = user_id
        device_id = str((data or {}).get("device_id") or "").strip()
        if device_id:
            self.config.device_id = device_id

    def _build_encrypted_payload(self, msg: OutboundMessage) -> dict | None:
        if not self.config.e2ee_enabled:
            return None
        payload = msg.metadata.get("matrix_encrypted_content")
        if isinstance(payload, dict) and payload:
            return payload
        if self.config.e2ee_require:
            logger.warning("Matrix E2E required but no encrypted payload provided")
        return None

    async def _send_via_nio_if_available(self, msg: OutboundMessage) -> bool:
        if not self._nio_client:
            return False
        try:
            content = self._build_text_payload(msg)
            await self._nio_client.room_send(  # type: ignore[union-attr]
                room_id=msg.chat_id,
                message_type="m.room.message",
                content=content,
                ignore_unverified_devices=True,
            )
            for media in msg.media:
                await self._send_media_event(msg.chat_id, media)
            return True
        except Exception as exc:
            logger.debug("Matrix nio send fallback to REST: {}", exc)
            return False

    async def _send_media_events(self, msg: OutboundMessage) -> None:
        for media in msg.media:
            await self._send_media_event(msg.chat_id, media)

    async def _send_media_event(self, chat_id: str, media: str) -> None:
        mxc_url, info = await self._resolve_media_to_mxc(media)
        if not mxc_url:
            return
        txn_id = f"nano-media-{int(asyncio.get_running_loop().time() * 1000)}"
        room_id = quote(chat_id, safe="")
        txn = quote(txn_id, safe="")
        payload = {
            "msgtype": info.get("msgtype", "m.file"),
            "body": info.get("body", "attachment"),
            "url": mxc_url,
            "info": info.get("info", {}),
        }
        await self._matrix_put(
            f"/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn}",
            payload,
        )

    async def _resolve_media_to_mxc(self, media: str) -> tuple[str, dict]:
        value = str(media or "").strip()
        if not value:
            return "", {}
        if value.startswith("mxc://"):
            return value, {"msgtype": "m.file", "body": value.rsplit("/", 1)[-1], "info": {}}

        path = None
        try:
            from pathlib import Path

            p = Path(value)
            if p.exists() and p.is_file():
                path = p
        except Exception:
            path = None
        if not path:
            return "", {}

        data = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        upload = await self._matrix_post_raw(
            "/_matrix/media/v3/upload",
            content=data,
            content_type=mime,
        )
        mxc_url = str(upload.get("content_uri") or "").strip()
        msgtype = "m.file"
        if mime.startswith("image/"):
            msgtype = "m.image"
        elif mime.startswith("audio/"):
            msgtype = "m.audio"
        elif mime.startswith("video/"):
            msgtype = "m.video"
        info = {"mimetype": mime, "size": len(data)}
        return mxc_url, {"msgtype": msgtype, "body": path.name, "info": info}

    def _build_text_payload(self, msg: OutboundMessage) -> dict:
        formatted = str(
            msg.metadata.get("matrix_formatted_body") or msg.metadata.get("formatted_body") or ""
        ).strip()
        if formatted:
            return {
                "msgtype": "m.text",
                "body": msg.content,
                "format": "org.matrix.custom.html",
                "formatted_body": formatted,
            }
        body_text = msg.content
        if msg.media:
            refs = "\n".join(f"[media_ref: {m}]" for m in msg.media)
            body_text = f"{body_text}\n{refs}"
        return {"msgtype": "m.text", "body": body_text}

    async def _maybe_init_nio_e2e(self) -> None:
        if not (self.config.e2ee_enabled and self.config.access_token and self.config.user_id):
            return
        try:
            import nio  # type: ignore
        except Exception:
            return
        try:
            store_path = str(self.media_root / "matrix-crypto")
            self._nio_client = nio.AsyncClient(
                self.config.homeserver, self.config.user_id, store_path=store_path
            )
            self._nio_client.access_token = self.config.access_token
            if self.config.device_id:
                self._nio_client.device_id = self.config.device_id
        except Exception as exc:
            logger.debug("Matrix nio e2e init failed: {}", exc)
            self._nio_client = None

    async def _download_mxc(self, mxc_url: str) -> str | None:
        if not self._http:
            self._http = httpx.AsyncClient(timeout=30.0)
        if not mxc_url.startswith("mxc://"):
            return None
        tail = mxc_url[len("mxc://") :]
        if "/" not in tail:
            return None
        server, media_id = tail.split("/", 1)
        url = f"{self.config.homeserver.rstrip('/')}/_matrix/media/v3/download/{quote(server, safe='')}/{quote(media_id, safe='')}"
        try:
            resp = await self._http.get(url, headers=self._headers())
            resp.raise_for_status()
            out_dir = self.media_root / "matrix"
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / media_id
            out.write_bytes(resp.content)
            return str(out)
        except Exception:
            return None

    @staticmethod
    def _mxc_to_media_ref(mxc_url: str, msgtype: str) -> str:
        tail = mxc_url[len("mxc://") :] if mxc_url.startswith("mxc://") else mxc_url
        media_id = tail.rsplit("/", 1)[-1] if tail else "file"
        media_type = "file"
        if msgtype == "m.image":
            media_type = "image"
        elif msgtype == "m.audio":
            media_type = "audio"
        elif msgtype == "m.video":
            media_type = "video"
        return f"media://matrix/{media_type}/{media_id}"

    def _check_e2ee_runtime(self) -> bool:
        if not self.config.e2ee_enabled:
            return False
        try:
            import olm  # type: ignore # noqa: F401

            return True
        except Exception:
            return False

    def _headers(self, optional: bool = False) -> dict[str, str]:
        token = self.config.access_token.strip()
        if not token and not optional:
            return {}
        return {"Authorization": f"Bearer {token}"} if token else {}

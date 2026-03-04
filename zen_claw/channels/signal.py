"""Signal channel implementation with signald/signal-cli bridge."""

from __future__ import annotations

import asyncio
import base64
import mimetypes
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from zen_claw.bus.events import OutboundMessage
from zen_claw.bus.queue import MessageBus
from zen_claw.channels.base import BaseChannel
from zen_claw.config.schema import SignalConfig


class SignalChannel(BaseChannel):
    """Signal channel bridge (signald HTTP API or signal-cli process)."""

    name = "signal"

    def __init__(self, config: SignalConfig, bus: MessageBus, media_root=None):
        super().__init__(config, bus, media_root=media_root)
        self.config: SignalConfig = config
        self._http: httpx.AsyncClient | None = None
        self._poll_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
        if self.config.mode == "signald":
            self._http = httpx.AsyncClient(timeout=20.0)
            self._poll_task = asyncio.create_task(self._signald_poll_loop())
        logger.info("Signal channel started in {} mode", self.config.mode)

    async def stop(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None
        if self._http:
            await self._http.aclose()
            self._http = None

    async def send(self, msg: OutboundMessage) -> None:
        try:
            if self.config.mode == "signald":
                await self._send_via_signald(msg)
            else:
                await self._send_via_signal_cli(msg)
        except Exception as exc:
            logger.warning("Signal send failed (mode={}): {}", self.config.mode, exc)

    async def _send_via_signald(self, msg: OutboundMessage) -> None:
        payload: dict[str, Any] = {
            "account": self.config.account,
            "recipientAddress": {"number": msg.chat_id},
            "messageBody": msg.content,
        }
        if msg.media:
            payload["attachments"] = [str(p) for p in msg.media if str(p).strip()]
        try:
            await self._signald_post("/v1/send", payload)
        except Exception as exc:
            if not self._is_not_found(exc):
                raise
            await self._signald_rpc_multi(
                ["send", "sendMessage", "send_message"],
                payload,
            )

    async def _send_via_signal_cli(self, msg: OutboundMessage) -> None:
        if not self.config.account:
            raise RuntimeError("signal_cli mode requires signal.account")
        argv = [self.config.signal_cli_bin, "-u", self.config.account, "send"]
        media = [str(p) for p in msg.media if str(p).strip()]
        for path in media:
            argv.extend(["-a", path])
        argv.extend(["-m", msg.content, msg.chat_id])
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"signal-cli exited {proc.returncode}: {stderr.decode(errors='replace')[:200]}")

    async def _signald_poll_loop(self) -> None:
        if not self.config.account:
            logger.warning("Signal signald receive loop disabled: signal.account not configured")
            return
        while self._running:
            try:
                try:
                    payload = await self._signald_get(f"/v1/receive/{self.config.account}")
                except Exception as exc:
                    if not self._is_not_found(exc):
                        raise
                    payload = await self._signald_rpc_multi(
                        ["receive", "receiveMessages", "receive_messages"],
                        {"account": self.config.account},
                    )
                await self._process_signald_payload(payload)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Signal poll tick failed: {}", exc)
            await asyncio.sleep(2.0)

    async def _process_signald_payload(self, payload: Any) -> None:
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            envelope = item.get("envelope") if isinstance(item, dict) else None
            if not isinstance(envelope, dict):
                continue
            source = str(envelope.get("source") or "")
            data = envelope.get("dataMessage") or {}
            if not isinstance(data, dict):
                continue
            body = str(data.get("message") or "").strip()
            if not source or not body:
                continue

            attachments = data.get("attachments") or []
            media_paths: list[str] = []
            media_refs: list[str] = []
            if isinstance(attachments, list):
                for idx, att in enumerate(attachments):
                    if isinstance(att, dict):
                        content_type = str(att.get("contentType") or "file")
                        aid = str(att.get("id") or idx)
                        ref_type = "image" if content_type.startswith("image/") else "file"
                        media_ref = self._build_media_uri("signal", ref_type, aid)
                        media_refs.append(media_ref)
                        path = str(att.get("path") or "").strip()
                        if path:
                            media_paths.append(str(Path(path)))
                        elif self.config.attachment_download:
                            downloaded = await self._download_signal_attachment(att, idx)
                            if downloaded:
                                media_paths.append(downloaded)

            await self._handle_message(
                sender_id=source,
                chat_id=source,
                content=body,
                media=media_paths,
                metadata={"media_refs": media_refs},
            )

    async def _signald_post(self, path: str, payload: dict[str, Any]) -> Any:
        if not self._http:
            self._http = httpx.AsyncClient(timeout=20.0)
        url = f"{self.config.signald_url.rstrip('/')}{path}"
        resp = await self._http.post(url, json=payload)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    async def _signald_get(self, path: str) -> Any:
        if not self._http:
            self._http = httpx.AsyncClient(timeout=20.0)
        url = f"{self.config.signald_url.rstrip('/')}{path}"
        resp = await self._http.get(url)
        resp.raise_for_status()
        return resp.json() if resp.content else []

    async def _signald_rpc(self, method: str, params: dict[str, Any]) -> Any:
        if not self._http:
            self._http = httpx.AsyncClient(timeout=20.0)
        path = self.config.signald_rpc_path
        if not path.startswith("/"):
            path = f"/{path}"
        url = f"{self.config.signald_url.rstrip('/')}{path}"
        payload = {"jsonrpc": "2.0", "id": "zen-claw", "method": method, "params": params}
        resp = await self._http.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        if isinstance(data, dict) and "result" in data:
            return data.get("result")
        return data

    async def _signald_rpc_multi(self, methods: list[str], params: dict[str, Any]) -> Any:
        last_exc: Exception | None = None
        for method in methods:
            try:
                return await self._signald_rpc(method, params)
            except Exception as exc:
                last_exc = exc
                continue
        if last_exc:
            raise last_exc
        return None

    async def _download_signal_attachment(self, att: dict[str, Any], idx: int) -> str | None:
        if not self._http:
            self._http = httpx.AsyncClient(timeout=20.0)
        aid = str(att.get("id") or idx).strip()
        if not aid:
            return None
        content_type = str(att.get("contentType") or "").strip().lower()
        ext = mimetypes.guess_extension(content_type) or ""
        file_dir = self.media_root / "signal"
        file_dir.mkdir(parents=True, exist_ok=True)
        out = file_dir / f"{aid}{ext}"

        inline_url = str(att.get("url") or att.get("remoteUrl") or "").strip()
        candidates: list[str] = []
        if inline_url:
            candidates.append(inline_url)
        base = self.config.signald_url.rstrip("/")
        if self.config.account:
            candidates.append(f"{base}/v1/attachments/{self.config.account}/{aid}")
            candidates.append(f"{base}/v1/attachments/{aid}?account={self.config.account}")
        candidates.append(f"{base}/v1/attachments/{aid}")

        for url in candidates:
            try:
                resp = await self._http.get(url)
                if resp.status_code == 404:
                    continue
                resp.raise_for_status()
                out.write_bytes(resp.content)
                return str(out)
            except Exception:
                continue
        try:
            rpc_data = await self._signald_rpc_multi(
                ["getAttachment", "attachmentGet", "get_attachment"],
                {"account": self.config.account, "id": aid},
            )
            if isinstance(rpc_data, dict):
                path = str(rpc_data.get("path") or "").strip()
                if path and Path(path).exists():
                    return str(Path(path))
                b64 = str(rpc_data.get("base64") or rpc_data.get("data") or "").strip()
                if b64:
                    out.write_bytes(base64.b64decode(b64))
                    return str(out)
                url = str(rpc_data.get("url") or "").strip()
                if url:
                    resp = await self._http.get(url)
                    resp.raise_for_status()
                    out.write_bytes(resp.content)
                    return str(out)
        except Exception:
            pass
        return None

    @staticmethod
    def _is_not_found(exc: Exception) -> bool:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
            return True
        msg = str(exc).lower()
        return "404" in msg or "not found" in msg

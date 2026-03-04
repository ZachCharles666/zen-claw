"""Generic external webhook trigger channel."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from zen_claw.bus.events import OutboundMessage
from zen_claw.bus.queue import MessageBus
from zen_claw.channels.base import BaseChannel
from zen_claw.config.loader import get_data_dir
from zen_claw.config.schema import WebhookTriggerConfig


class _NonceStore:
    """SQLite-backed nonce cache with TTL-based replay protection."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS webhook_nonce_cache (
                    nonce TEXT PRIMARY KEY,
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.commit()

    def mark_once(self, nonce: str, now_unix: int, ttl_sec: int) -> bool:
        """Return False when nonce exists (replay), True when first-seen."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM webhook_nonce_cache WHERE created_at < ?",
                (int(now_unix - max(1, ttl_sec)),),
            )
            row = conn.execute(
                "SELECT nonce FROM webhook_nonce_cache WHERE nonce = ? LIMIT 1",
                (nonce,),
            ).fetchone()
            if row is not None:
                conn.commit()
                return False
            conn.execute(
                "INSERT INTO webhook_nonce_cache (nonce, created_at) VALUES (?, ?)",
                (nonce, int(now_unix)),
            )
            conn.commit()
            return True


class WebhookTriggerChannel(BaseChannel):
    """Parse signed external webhook payloads and inject InboundMessage."""

    name = "webhook_trigger"

    def __init__(self, config: WebhookTriggerConfig, bus: MessageBus, media_root=None):
        super().__init__(config, bus, media_root=media_root)
        self.config: WebhookTriggerConfig = config
        data_dir = get_data_dir()
        self._nonce_store = _NonceStore(data_dir / "webhook" / "nonce_cache.sqlite3")

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg: OutboundMessage) -> None:
        # Trigger channel does not send outbound messages.
        _ = msg

    def _ip_allowed(self, client_ip: str) -> bool:
        ip_s = str(client_ip or "").strip()
        if not ip_s:
            return False
        try:
            ip_obj = ipaddress.ip_address(ip_s)
        except ValueError:
            return False
        for rule in self.config.ip_allowlist:
            token = str(rule or "").strip()
            if not token:
                continue
            try:
                if "/" in token:
                    if ip_obj in ipaddress.ip_network(token, strict=False):
                        return True
                elif ip_obj == ipaddress.ip_address(token):
                    return True
            except ValueError:
                continue
        return False

    def _verify_signature(self, body: bytes, timestamp: str, nonce: str, signature: str) -> bool:
        secret = str(self.config.secret or "").strip()
        if not secret:
            return False
        message = f"{timestamp}.{nonce}.".encode("utf-8") + body
        expected = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, str(signature or "").strip())

    def validate_request(self, *, body: bytes, headers: dict[str, str], client_ip: str) -> tuple[bool, str]:
        """Validate request auth headers and anti-replay rules."""
        if self.config.allow_unsigned_from_allowlist and self._ip_allowed(client_ip):
            return True, ""

        api_key = str(headers.get("x-api-key", "")).strip()
        if self.config.api_key and api_key and hmac.compare_digest(api_key, self.config.api_key):
            return True, ""

        signature = str(headers.get("x-signature", "")).strip()
        timestamp = str(headers.get("x-timestamp", "")).strip()
        nonce = str(headers.get("x-nonce", "")).strip()
        if not signature or not timestamp or not nonce:
            return False, "missing_signature_headers"

        try:
            ts = int(timestamp)
        except ValueError:
            return False, "invalid_timestamp"
        now = int(time.time())
        tolerance = max(1, int(self.config.timestamp_tolerance_sec))
        if abs(now - ts) > tolerance:
            return False, "timestamp_out_of_window"
        if not self._verify_signature(body, timestamp, nonce, signature):
            return False, "invalid_signature"
        if not self._nonce_store.mark_once(nonce=nonce, now_unix=now, ttl_sec=int(self.config.nonce_ttl_sec)):
            return False, "replayed_nonce"
        return True, ""

    @staticmethod
    def _payload_to_content(payload: Any) -> str:
        if isinstance(payload, dict):
            for key in ("content", "message", "text", "prompt"):
                val = payload.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
            return json.dumps(payload, ensure_ascii=False)
        if isinstance(payload, str) and payload.strip():
            return payload.strip()
        return ""

    async def ingest_trigger(
        self,
        *,
        agent_id: str,
        payload: Any,
        query: dict[str, str] | None = None,
        client_ip: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Publish webhook trigger payload as an inbound message."""
        query = query or {}
        content = self._payload_to_content(payload) or self._payload_to_content(query)
        if not content:
            content = f"trigger:{agent_id}"
        sender_id = (
            str((payload or {}).get("sender_id", "")).strip()
            if isinstance(payload, dict)
            else ""
        ) or f"webhook:{agent_id}"
        chat_id = (
            str((payload or {}).get("chat_id", "")).strip()
            if isinstance(payload, dict)
            else ""
        ) or str(agent_id)
        meta = dict(metadata or {})
        meta["trigger_agent_id"] = str(agent_id)
        if client_ip:
            meta["client_ip"] = client_ip
        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content=content,
            metadata=meta,
        )


"""Persistent multi-agent routing store for channel/user sessions."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class AgentRoute:
    """Current route binding for one `{channel}:{chat_id}:{user_id}` key."""

    route_key: str
    channel: str
    chat_id: str
    user_id: str
    agent_id: str
    updated_at_ms: int


class AgentRouteStore:
    """SQLite-backed route mapping with immutable audit trail."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self._init_db()

    @staticmethod
    def make_route_key(channel: str, chat_id: str, user_id: str) -> str:
        ch = str(channel or "").strip().lower()
        chat = str(chat_id or "").strip()
        user = str(user_id or "").strip()
        if not ch or not chat or not user:
            raise ValueError("channel/chat_id/user_id are required")
        return f"{ch}:{chat}:{user}"

    def set_route(
        self,
        *,
        channel: str,
        chat_id: str,
        user_id: str,
        agent_id: str,
        reason: str = "manual_bind",
        at_ms: int | None = None,
    ) -> AgentRoute:
        route_key = self.make_route_key(channel, chat_id, user_id)
        new_agent = str(agent_id or "").strip()
        if not new_agent:
            raise ValueError("agent_id is required")
        now = int(at_ms if at_ms is not None else _now_ms())
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT agent_id, updated_at_ms FROM routes WHERE route_key = ?",
                (route_key,),
            ).fetchone()
            prev_agent = str(row[0]) if row else ""
            prev_updated = int(row[1]) if row else 0
            # LWW: skip stale updates.
            if row and now < prev_updated:
                return AgentRoute(
                    route_key=route_key,
                    channel=str(channel).lower(),
                    chat_id=str(chat_id),
                    user_id=str(user_id),
                    agent_id=prev_agent,
                    updated_at_ms=prev_updated,
                )
            conn.execute(
                """
                INSERT INTO routes(route_key, channel, chat_id, user_id, agent_id, updated_at_ms)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(route_key) DO UPDATE SET
                    channel=excluded.channel,
                    chat_id=excluded.chat_id,
                    user_id=excluded.user_id,
                    agent_id=excluded.agent_id,
                    updated_at_ms=excluded.updated_at_ms
                """,
                (
                    route_key,
                    str(channel).lower(),
                    str(chat_id),
                    str(user_id),
                    new_agent,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO route_audit(route_key, previous_agent_id, new_agent_id, reason, at_ms)
                VALUES(?, ?, ?, ?, ?)
                """,
                (route_key, prev_agent, new_agent, str(reason or "").strip(), now),
            )
            return AgentRoute(
                route_key=route_key,
                channel=str(channel).lower(),
                chat_id=str(chat_id),
                user_id=str(user_id),
                agent_id=new_agent,
                updated_at_ms=now,
            )

    def resolve_route(self, *, channel: str, chat_id: str, user_id: str) -> AgentRoute | None:
        route_key = self.make_route_key(channel, chat_id, user_id)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT route_key, channel, chat_id, user_id, agent_id, updated_at_ms
                FROM routes WHERE route_key = ?
                """,
                (route_key,),
            ).fetchone()
        if not row:
            return None
        return AgentRoute(
            route_key=str(row[0]),
            channel=str(row[1]),
            chat_id=str(row[2]),
            user_id=str(row[3]),
            agent_id=str(row[4]),
            updated_at_ms=int(row[5]),
        )

    def soft_rollback_on_error(
        self,
        *,
        channel: str,
        chat_id: str,
        user_id: str,
        current_agent_id: str,
        grace_period_ms: int = 180_000,
        now_ms: int | None = None,
    ) -> AgentRoute | None:
        """
        Attempt bounded rollback to previous route when current route is failing.

        Returns rollback route when applied, otherwise None.
        """
        route_key = self.make_route_key(channel, chat_id, user_id)
        now = int(now_ms if now_ms is not None else _now_ms())
        cutoff = now - max(1, int(grace_period_ms))
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT previous_agent_id, at_ms
                FROM route_audit
                WHERE route_key = ? AND new_agent_id = ? AND at_ms >= ? AND previous_agent_id != ''
                ORDER BY at_ms DESC
                LIMIT 1
                """,
                (route_key, str(current_agent_id or "").strip(), cutoff),
            ).fetchone()
            if not row:
                return None
            rollback_agent = str(row[0]).strip()
            if not rollback_agent:
                return None
            route = self.set_route(
                channel=channel,
                chat_id=chat_id,
                user_id=user_id,
                agent_id=rollback_agent,
                reason="soft_rollback",
                at_ms=now,
            )
            return route

    def list_audit(self, *, channel: str, chat_id: str, user_id: str) -> list[dict[str, int | str]]:
        route_key = self.make_route_key(channel, chat_id, user_id)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT route_key, previous_agent_id, new_agent_id, reason, at_ms
                FROM route_audit
                WHERE route_key = ?
                ORDER BY id ASC
                """,
                (route_key,),
            ).fetchall()
        out: list[dict[str, int | str]] = []
        for r in rows:
            out.append(
                {
                    "route_key": str(r[0]),
                    "previous_agent_id": str(r[1]),
                    "new_agent_id": str(r[2]),
                    "reason": str(r[3]),
                    "at_ms": int(r[4]),
                }
            )
        return out

    def gc_expired_routes(self, *, ttl_ms: int, now_ms: int | None = None) -> int:
        now = int(now_ms if now_ms is not None else _now_ms())
        cutoff = now - max(1, int(ttl_ms))
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM routes WHERE updated_at_ms < ?", (cutoff,))
            return int(cur.rowcount or 0)

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS routes(
                    route_key TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS route_audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    route_key TEXT NOT NULL,
                    previous_agent_id TEXT NOT NULL,
                    new_agent_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    at_ms INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_route_audit_key_time ON route_audit(route_key, at_ms)"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

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

    def clear_route(
        self,
        *,
        channel: str,
        chat_id: str,
        user_id: str,
        reason: str = "manual_clear",
        at_ms: int | None = None,
    ) -> dict[str, int | str]:
        route_key = self.make_route_key(channel, chat_id, user_id)
        now = int(at_ms if at_ms is not None else _now_ms())
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT agent_id FROM routes WHERE route_key = ?",
                (route_key,),
            ).fetchone()
            prev_agent = str(row[0]) if row else ""
            if row:
                conn.execute("DELETE FROM routes WHERE route_key = ?", (route_key,))
            conn.execute(
                """
                INSERT INTO route_audit(route_key, previous_agent_id, new_agent_id, reason, at_ms)
                VALUES(?, ?, ?, ?, ?)
                """,
                (route_key, prev_agent, "", str(reason or "").strip(), now),
            )
        return {
            "route_key": route_key,
            "previous_agent_id": prev_agent,
            "new_agent_id": "",
            "reason": str(reason or "").strip(),
            "at_ms": now,
        }

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
                ORDER BY at_ms ASC, id ASC
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

    def list_routes(
        self,
        *,
        channel: str = "",
        agent_id: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, int | str]]:
        """List all active sticky route bindings with optional filters."""
        norm_channel = str(channel or "").strip().lower()
        norm_agent = str(agent_id or "").strip()
        bounded_limit = max(1, min(int(limit), 200))
        bounded_offset = max(0, int(offset))
        query = "SELECT route_key, channel, chat_id, user_id, agent_id, updated_at_ms FROM routes WHERE 1=1"
        params: list[str | int] = []
        if norm_channel:
            query += " AND channel = ?"
            params.append(norm_channel)
        if norm_agent:
            query += " AND agent_id = ?"
            params.append(norm_agent)
        query += " ORDER BY updated_at_ms DESC LIMIT ? OFFSET ?"
        params.extend([bounded_limit, bounded_offset])
        with self._lock, self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "route_key": str(r[0]),
                "channel": str(r[1]),
                "chat_id": str(r[2]),
                "user_id": str(r[3]),
                "agent_id": str(r[4]),
                "updated_at_ms": int(r[5]),
            }
            for r in rows
        ]

    @staticmethod
    def _parse_route_key(route_key: str) -> tuple[str, str, str]:
        """Split route_key into (channel, chat_id, user_id). Tolerates colons in user_id."""
        parts = str(route_key).split(":", 2)
        channel_part = parts[0] if len(parts) > 0 else ""
        chat_part = parts[1] if len(parts) > 1 else ""
        user_part = parts[2] if len(parts) > 2 else ""
        return channel_part, chat_part, user_part

    def list_audit_all(
        self,
        *,
        channel: str = "",
        agent_id: str = "",
        reason: str = "",
        from_at_ms: int = 0,
        to_at_ms: int = 0,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, int | str]]:
        """List route audit entries across all route keys with optional filters."""
        norm_channel = str(channel or "").strip().lower()
        norm_agent = str(agent_id or "").strip()
        norm_reason = str(reason or "").strip()
        bounded_limit = max(1, min(int(limit), 200))
        bounded_offset = max(0, int(offset))
        bounded_from = max(0, int(from_at_ms or 0))
        bounded_to = max(0, int(to_at_ms or 0))

        query = """
            SELECT route_key, previous_agent_id, new_agent_id, reason, at_ms
            FROM route_audit WHERE 1=1
        """
        params: list[str | int] = []
        # channel filter: route_key starts with "{channel}:"
        if norm_channel:
            query += " AND (route_key = ? OR route_key LIKE ?)"
            params.append(norm_channel)
            params.append(norm_channel + ":%")
        if norm_agent:
            query += " AND (previous_agent_id = ? OR new_agent_id = ?)"
            params.extend([norm_agent, norm_agent])
        if norm_reason:
            query += " AND reason = ?"
            params.append(norm_reason)
        if bounded_from:
            query += " AND at_ms >= ?"
            params.append(bounded_from)
        if bounded_to:
            query += " AND at_ms <= ?"
            params.append(bounded_to)
        query += " ORDER BY at_ms DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([bounded_limit, bounded_offset])

        with self._lock, self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        out: list[dict[str, int | str]] = []
        for r in rows:
            rk = str(r[0])
            ch, chat, user = self._parse_route_key(rk)
            out.append(
                {
                    "route_key": rk,
                    "channel": ch,
                    "chat_id": chat,
                    "user_id": user,
                    "previous_agent_id": str(r[1]),
                    "new_agent_id": str(r[2]),
                    "reason": str(r[3]),
                    "at_ms": int(r[4]),
                }
            )
        return out

    def clear_routes_by(
        self,
        *,
        channel: str = "",
        agent_id: str = "",
        reason: str = "bulk_clear",
        dry_run: bool = False,
    ) -> dict[str, object]:
        """Bulk-clear route bindings matching channel and/or agent_id.

        At least one of *channel* or *agent_id* must be provided to prevent
        accidental full-table wipes.  When *dry_run=True* returns the list of
        routes that *would* be cleared without modifying any data.
        """
        norm_channel = str(channel or "").strip().lower()
        norm_agent = str(agent_id or "").strip()
        if not norm_channel and not norm_agent:
            raise ValueError("At least one of channel or agent_id is required for bulk clear")

        # Find matching routes.
        matching = self.list_routes(channel=norm_channel, agent_id=norm_agent, limit=200, offset=0)

        if dry_run:
            return {"cleared": 0, "preview": matching, "dry_run": True}

        now = _now_ms()
        cleared = 0
        norm_reason = str(reason or "bulk_clear").strip()
        with self._lock, self._connect() as conn:
            for row in matching:
                rk = str(row["route_key"])
                prev_agent = str(row["agent_id"])
                conn.execute("DELETE FROM routes WHERE route_key = ?", (rk,))
                conn.execute(
                    """
                    INSERT INTO route_audit(route_key, previous_agent_id, new_agent_id, reason, at_ms)
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    (rk, prev_agent, "", norm_reason, now),
                )
                cleared += 1
        return {"cleared": cleared, "preview": [], "dry_run": False}

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

"""SQLite-backed conversation history store — A4."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Session:
    """A conversation session (one continuous exchange with an agent)."""

    id: str
    agent_id: str
    channel: str
    tenant_id: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class Message:
    """A single message within a session."""

    id: str
    session_id: str
    role: str          # "user" | "assistant" | "system"
    content: str
    tokens: int = 0
    at_ms: int = field(default_factory=lambda: int(time.time() * 1000))


class ConversationStore:
    """
    SQLite-backed store for conversation sessions and messages.

    Database: {data_dir}/conversations.db
    Tables:
      - sessions(id, agent_id, channel, tenant_id, created_at)
      - messages(id, session_id, role, content, tokens, at_ms)
    """

    DB_FILENAME = "conversations.db"

    def __init__(self, data_dir: Path, *, redact_content: bool = False) -> None:
        self._db_path = Path(data_dir) / self.DB_FILENAME
        self._redact = redact_content
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id         TEXT PRIMARY KEY,
                    agent_id   TEXT NOT NULL,
                    channel    TEXT NOT NULL DEFAULT '',
                    tenant_id  TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id         TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    role       TEXT NOT NULL,
                    content    TEXT NOT NULL DEFAULT '',
                    tokens     INTEGER NOT NULL DEFAULT 0,
                    at_ms      INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_agent   ON sessions(agent_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_channel ON sessions(channel);
            """)

    # ── Sessions ──────────────────────────────────────────────────────────────

    def upsert_session(self, session: Session) -> None:
        """Insert or update a session record."""
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO sessions (id, agent_id, channel, tenant_id, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (session.id, session.agent_id, session.channel, session.tenant_id, session.created_at),
            )

    def get_session(self, session_id: str) -> Session | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            return None
        return Session(
            id=row["id"],
            agent_id=row["agent_id"],
            channel=row["channel"],
            tenant_id=row["tenant_id"],
            created_at=row["created_at"],
        )

    def list_sessions(
        self,
        *,
        agent_id: str = "",
        channel: str = "",
        tenant_id: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Session], int]:
        """Returns (sessions, total_count)."""
        conditions: list[str] = []
        params: list[Any] = []
        if agent_id:
            conditions.append("agent_id=?")
            params.append(agent_id)
        if channel:
            conditions.append("channel=?")
            params.append(channel)
        if tenant_id:
            conditions.append("tenant_id=?")
            params.append(tenant_id)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        with self._connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM sessions {where}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM sessions {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()

        sessions = [
            Session(id=r["id"], agent_id=r["agent_id"], channel=r["channel"],
                    tenant_id=r["tenant_id"], created_at=r["created_at"])
            for r in rows
        ]
        return sessions, total

    # ── Messages ──────────────────────────────────────────────────────────────

    def add_message(self, message: Message) -> None:
        """Append a message to a session."""
        content = "[redacted]" if self._redact else message.content
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO messages (id, session_id, role, content, tokens, at_ms) VALUES (?,?,?,?,?,?)",
                (message.id, message.session_id, message.role, content, message.tokens, message.at_ms),
            )

    def get_messages(self, session_id: str) -> list[Message]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id=? ORDER BY at_ms",
                (session_id,),
            ).fetchall()
        return [
            Message(id=r["id"], session_id=r["session_id"], role=r["role"],
                    content=r["content"], tokens=r["tokens"], at_ms=r["at_ms"])
            for r in rows
        ]

    # ── Export ────────────────────────────────────────────────────────────────

    def export_json(self, session_id: str) -> str:
        """Export session + messages as a JSON string."""
        session = self.get_session(session_id)
        messages = self.get_messages(session_id)
        data = {
            "session": {
                "id": session.id,
                "agent_id": session.agent_id,
                "channel": session.channel,
                "tenant_id": session.tenant_id,
                "created_at": session.created_at,
            } if session else None,
            "messages": [
                {"id": m.id, "role": m.role, "content": m.content,
                 "tokens": m.tokens, "at_ms": m.at_ms}
                for m in messages
            ],
        }
        return json.dumps(data, ensure_ascii=False, indent=2)

    def export_csv(self, session_id: str) -> str:
        """Export messages as CSV string."""
        messages = self.get_messages(session_id)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id", "session_id", "role", "content", "tokens", "at_ms"])
        for m in messages:
            writer.writerow([m.id, m.session_id, m.role, m.content, m.tokens, m.at_ms])
        return buf.getvalue()

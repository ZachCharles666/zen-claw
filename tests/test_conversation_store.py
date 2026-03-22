"""Tests for A4 — Conversation history SQLite store."""

import csv
import io
import json
import time
import uuid
from pathlib import Path

import pytest

from zen_claw.history.store import ConversationStore, Message, Session


def _sid() -> str:
    return uuid.uuid4().hex[:12]


def _mid() -> str:
    return uuid.uuid4().hex[:16]


def _session(agent_id="default", channel="telegram", tenant_id="") -> Session:
    return Session(id=_sid(), agent_id=agent_id, channel=channel, tenant_id=tenant_id)


# ── DB initialization ─────────────────────────────────────────────────────────

def test_store_creates_db_file(tmp_path: Path) -> None:
    ConversationStore(tmp_path)
    assert (tmp_path / "conversations.db").exists()


def test_store_init_idempotent(tmp_path: Path) -> None:
    """Creating store twice should not raise."""
    ConversationStore(tmp_path)
    ConversationStore(tmp_path)


# ── Sessions ──────────────────────────────────────────────────────────────────

def test_upsert_and_get_session(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path)
    s = _session(agent_id="bot1", channel="discord")
    store.upsert_session(s)

    got = store.get_session(s.id)
    assert got is not None
    assert got.agent_id == "bot1"
    assert got.channel == "discord"


def test_get_session_not_found_returns_none(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path)
    assert store.get_session("nonexistent") is None


def test_list_sessions_pagination(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path)
    for i in range(5):
        s = Session(id=f"sess{i}", agent_id="bot", channel="telegram", created_at=float(i))
        store.upsert_session(s)

    sessions, total = store.list_sessions(limit=3, offset=0)
    assert total == 5
    assert len(sessions) == 3


def test_list_sessions_filter_by_agent(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path)
    store.upsert_session(_session(agent_id="agent_a"))
    store.upsert_session(_session(agent_id="agent_b"))
    store.upsert_session(_session(agent_id="agent_a"))

    sessions, total = store.list_sessions(agent_id="agent_a")
    assert total == 2
    for s in sessions:
        assert s.agent_id == "agent_a"


def test_list_sessions_filter_by_channel(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path)
    store.upsert_session(_session(channel="telegram"))
    store.upsert_session(_session(channel="discord"))

    sessions, total = store.list_sessions(channel="telegram")
    assert total == 1
    assert sessions[0].channel == "telegram"


# ── Messages ──────────────────────────────────────────────────────────────────

def test_add_and_get_messages(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path)
    s = _session()
    store.upsert_session(s)

    m1 = Message(id=_mid(), session_id=s.id, role="user", content="Hello", tokens=5)
    m2 = Message(id=_mid(), session_id=s.id, role="assistant", content="Hi!", tokens=10)
    store.add_message(m1)
    store.add_message(m2)

    msgs = store.get_messages(s.id)
    assert len(msgs) == 2
    assert msgs[0].role == "user"
    assert msgs[1].role == "assistant"


def test_add_message_empty_session_returns_empty(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path)
    s = _session()
    store.upsert_session(s)
    assert store.get_messages(s.id) == []


def test_redact_content_stores_placeholder(tmp_path: Path) -> None:
    """redact_content=True should store '[redacted]' instead of actual content."""
    store = ConversationStore(tmp_path, redact_content=True)
    s = _session()
    store.upsert_session(s)
    m = Message(id=_mid(), session_id=s.id, role="user", content="Secret message")
    store.add_message(m)

    msgs = store.get_messages(s.id)
    assert msgs[0].content == "[redacted]"


# ── Export ────────────────────────────────────────────────────────────────────

def test_export_json(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path)
    s = _session(agent_id="export_agent", channel="test")
    store.upsert_session(s)
    store.add_message(Message(id=_mid(), session_id=s.id, role="user", content="Hello", tokens=3))

    json_str = store.export_json(s.id)
    data = json.loads(json_str)
    assert data["session"]["id"] == s.id
    assert len(data["messages"]) == 1
    assert data["messages"][0]["role"] == "user"


def test_export_csv(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path)
    s = _session()
    store.upsert_session(s)
    store.add_message(Message(id=_mid(), session_id=s.id, role="user", content="Hi", tokens=2))
    store.add_message(Message(id=_mid(), session_id=s.id, role="assistant", content="Hello", tokens=5))

    csv_str = store.export_csv(s.id)
    reader = csv.reader(io.StringIO(csv_str))
    rows = list(reader)
    # header + 2 messages
    assert rows[0] == ["id", "session_id", "role", "content", "tokens", "at_ms"]
    assert len(rows) == 3


def test_export_csv_role_column(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path)
    s = _session()
    store.upsert_session(s)
    store.add_message(Message(id=_mid(), session_id=s.id, role="assistant", content="Response", tokens=10))

    csv_str = store.export_csv(s.id)
    reader = csv.DictReader(io.StringIO(csv_str))
    rows = list(reader)
    assert rows[0]["role"] == "assistant"
    assert rows[0]["content"] == "Response"

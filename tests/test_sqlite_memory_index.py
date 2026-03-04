"""Tests for SqliteMemoryIndex — FTS5 transaction atomicity (MEDIUM-002)."""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

from zen_claw.agent.memory_sqlite import SqliteMemoryIndex

# ── helpers ───────────────────────────────────────────────────────────────────


def _index(tmp_path: Path) -> SqliteMemoryIndex:
    return SqliteMemoryIndex(db_path=tmp_path / "mem.db")


def _fts5_contents(db_path: Path) -> list[str]:
    """Read FTS5 table directly from the DB file (separate connection)."""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute("SELECT content FROM memory_fts")
        return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def _embeddings_contents(db_path: Path) -> list[str]:
    """Read memory_embeddings table from the DB file."""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute("SELECT content FROM memory_embeddings")
        return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


# ── tests: FTS5 transaction atomicity ─────────────────────────────────────────


def test_fts5_sync_committed_without_embedder(tmp_path: Path):
    """
    FTS5 sync must be persisted even when no embedder is available.
    This was the core MEDIUM-002 bug: conn.commit() was only inside
    `if missing and embedder:`, so without an embedder the DELETE+INSERT
    was always rolled back on conn.close().
    """
    idx = _index(tmp_path)
    # No embedder in test env (rag deps not installed) → _embedder stays None
    idx._embedder_loaded = True  # skip auto-load attempt
    idx._embedder = None

    candidates = ["memory about cats", "memory about dogs"]
    idx.sync_and_search("cats", candidates)

    stored = _fts5_contents(tmp_path / "mem.db")
    assert set(stored) == set(candidates), (
        f"FTS5 table should contain all candidates even without embedder; got: {stored}"
    )


def test_fts5_sync_committed_when_embed_raises(tmp_path: Path):
    """
    If embedding raises an exception, the FTS5 sync must still be committed.
    The embedding failure should only affect the embeddings table, not the FTS5 index.
    """
    idx = _index(tmp_path)
    mock_embedder = MagicMock()
    mock_embedder.embed.side_effect = RuntimeError("embed service unavailable")
    idx._embedder = mock_embedder
    idx._embedder_loaded = True

    candidates = ["document alpha", "document beta"]
    idx.sync_and_search("alpha", candidates)

    stored = _fts5_contents(tmp_path / "mem.db")
    assert set(stored) == set(candidates), (
        "FTS5 table should be committed even when embedding fails"
    )
    # Embeddings table should remain empty because embed() raised
    assert _embeddings_contents(tmp_path / "mem.db") == []


def test_fts5_sync_replaces_all_previous_entries(tmp_path: Path):
    """
    Second call with different candidates should replace all FTS5 entries
    from the first call — DELETE is committed together with INSERT.
    """
    idx = _index(tmp_path)
    idx._embedder_loaded = True
    idx._embedder = None

    # First sync
    idx.sync_and_search("foo", ["entry-A", "entry-B", "entry-C"])
    assert set(_fts5_contents(tmp_path / "mem.db")) == {"entry-A", "entry-B", "entry-C"}

    # Second sync with completely different candidates
    idx.sync_and_search("foo", ["entry-X", "entry-Y"])
    stored = _fts5_contents(tmp_path / "mem.db")
    assert set(stored) == {"entry-X", "entry-Y"}, (
        f"Old FTS5 entries should be replaced; got: {stored}"
    )


def test_fts5_sync_with_successful_embedder(tmp_path: Path):
    """
    Happy path: embedder succeeds → both FTS5 table and embeddings table
    should be populated and committed.
    """
    idx = _index(tmp_path)
    mock_embedder = MagicMock()
    # Return a fake embedding vector for each candidate
    mock_embedder.embed.side_effect = lambda texts: [[0.1, 0.2] for _ in texts]
    idx._embedder = mock_embedder
    idx._embedder_loaded = True

    candidates = ["vec candidate A", "vec candidate B"]
    idx.sync_and_search("candidate", candidates)

    assert set(_fts5_contents(tmp_path / "mem.db")) == set(candidates)
    assert set(_embeddings_contents(tmp_path / "mem.db")) == set(candidates)


def test_connection_always_closed(tmp_path: Path, monkeypatch):
    """
    conn.close() must be called via try/finally even during a normal run.
    We patch sqlite3.connect (only for sync_and_search, not _init_db) with a
    MagicMock so we can assert close() was called.
    """
    import zen_claw.agent.memory_sqlite as mod

    # Build the index (uses the real sqlite3 for _init_db)
    idx = _index(tmp_path)
    idx._embedder_loaded = True
    idx._embedder = None

    # Now swap sqlite3.connect with a MagicMock factory
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_cur.fetchall.return_value = []
    # Make `with conn:` work: __enter__ returns mock_conn, __exit__ returns False
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.__exit__.return_value = False

    monkeypatch.setattr(mod.sqlite3, "connect", lambda *a, **kw: mock_conn)

    idx.sync_and_search("test", ["item-1"])

    mock_conn.close.assert_called_once()


def test_empty_candidates_returns_early_without_touching_db(tmp_path: Path):
    """Passing empty candidates must return [] without modifying the DB."""
    idx = _index(tmp_path)
    result = idx.sync_and_search("query", [])
    assert result == []
    # FTS5 table should still be empty (no spurious writes)
    assert _fts5_contents(tmp_path / "mem.db") == []


def test_sync_and_search_returns_fts5_match(tmp_path: Path):
    """Basic sanity: a candidate matching the query should appear in results."""
    idx = _index(tmp_path)
    idx._embedder_loaded = True
    idx._embedder = None

    candidates = ["the quick brown fox", "lazy dog sleeps", "quick fox jumps"]
    results = idx.sync_and_search("quick fox", candidates)

    result_contents = [c for _, c in results]
    assert len(result_contents) > 0
    # At least one matching candidate should be returned
    assert any("fox" in c or "quick" in c for c in result_contents)

"""Tests for DatabaseMigrateTool, DatabaseInspectTool, DatabaseQueryTool, DatabaseExecuteTool."""

import json
import sqlite3
from pathlib import Path

import pytest

from zen_claw.agent.tools.database import (
    DatabaseExecuteTool,
    DatabaseInspectTool,
    DatabaseMigrateTool,
    DatabaseQueryTool,
)

# ── helpers ───────────────────────────────────────────────────────────────────

def _create_db(workspace: Path, name: str = "test.db") -> Path:
    db = workspace / name
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    conn.execute("INSERT INTO users VALUES (1, 'Alice'), (2, 'Bob')")
    conn.commit()
    conn.close()
    return db


# ── DatabaseMigrateTool tests ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_migrate_applies_new_migration(tmp_path: Path):
    _create_db(tmp_path)
    tool = DatabaseMigrateTool(workspace=tmp_path)
    result = await tool.execute(
        db_path="test.db",
        migration_id="001_add_posts",
        sql="CREATE TABLE posts (id INTEGER PRIMARY KEY, title TEXT)",
    )
    assert result.ok, result
    data = json.loads(result.content)
    assert data["status"] == "applied"
    assert data["statements_run"] == 1


@pytest.mark.asyncio
async def test_migrate_skips_already_applied(tmp_path: Path):
    _create_db(tmp_path)
    tool = DatabaseMigrateTool(workspace=tmp_path)
    await tool.execute(
        db_path="test.db",
        migration_id="001_add_posts",
        sql="CREATE TABLE posts (id INTEGER PRIMARY KEY, title TEXT)",
    )
    # Second run should be skipped
    result = await tool.execute(
        db_path="test.db",
        migration_id="001_add_posts",
        sql="CREATE TABLE posts (id INTEGER PRIMARY KEY, title TEXT)",
    )
    assert result.ok
    data = json.loads(result.content)
    assert data["status"] == "skipped"
    assert data["reason"] == "already_applied"


@pytest.mark.asyncio
async def test_migrate_rolls_back_on_error(tmp_path: Path):
    _create_db(tmp_path)
    tool = DatabaseMigrateTool(workspace=tmp_path)
    result = await tool.execute(
        db_path="test.db",
        migration_id="bad_migration",
        sql="CREATE TABLE ok_table (id INT); THIS IS NOT VALID SQL!!!",
    )
    assert not result.ok
    assert "rolled back" in result.error.message


@pytest.mark.asyncio
async def test_migrate_blocks_drop_by_default(tmp_path: Path):
    _create_db(tmp_path)
    tool = DatabaseMigrateTool(workspace=tmp_path)
    result = await tool.execute(
        db_path="test.db",
        migration_id="drop_users",
        sql="DROP TABLE users",
    )
    assert not result.ok
    assert result.error.code == "db_migration_destructive_denied"


@pytest.mark.asyncio
async def test_migrate_allows_drop_when_flagged(tmp_path: Path):
    _create_db(tmp_path)
    tool = DatabaseMigrateTool(workspace=tmp_path)
    result = await tool.execute(
        db_path="test.db",
        migration_id="drop_users",
        sql="DROP TABLE users",
        allow_destructive=True,
    )
    assert result.ok
    data = json.loads(result.content)
    assert data["status"] == "applied"


@pytest.mark.asyncio
async def test_migrate_multi_statement(tmp_path: Path):
    _create_db(tmp_path)
    tool = DatabaseMigrateTool(workspace=tmp_path)
    sql = (
        "CREATE TABLE tags (id INTEGER PRIMARY KEY, label TEXT);"
        "CREATE TABLE post_tags (post_id INT, tag_id INT)"
    )
    result = await tool.execute(db_path="test.db", migration_id="002_tags", sql=sql)
    assert result.ok
    data = json.loads(result.content)
    assert data["statements_run"] == 2


@pytest.mark.asyncio
async def test_migrate_rejects_path_outside_workspace(tmp_path: Path):
    tool = DatabaseMigrateTool(workspace=tmp_path)
    result = await tool.execute(
        db_path="/etc/passwords.db",
        migration_id="bad",
        sql="CREATE TABLE x (id INT)",
    )
    assert not result.ok
    assert result.error.code == "db_path_outside_workspace"


# ── DatabaseInspectTool tests ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_inspect_returns_table_info(tmp_path: Path):
    _create_db(tmp_path)
    tool = DatabaseInspectTool(workspace=tmp_path)
    result = await tool.execute(db_path="test.db")
    assert result.ok, result
    data = json.loads(result.content)
    assert data["table_count"] >= 1
    users = next(t for t in data["tables"] if t["table"] == "users")
    assert users["row_count"] == 2
    assert "name" in users["columns"]


@pytest.mark.asyncio
async def test_inspect_returns_size_info(tmp_path: Path):
    _create_db(tmp_path)
    tool = DatabaseInspectTool(workspace=tmp_path)
    result = await tool.execute(db_path="test.db")
    assert result.ok
    data = json.loads(result.content)
    assert data["size_bytes"] > 0
    assert "B" in data["size_human"] or "KB" in data["size_human"]


@pytest.mark.asyncio
async def test_inspect_db_not_found(tmp_path: Path):
    tool = DatabaseInspectTool(workspace=tmp_path)
    result = await tool.execute(db_path="nonexistent.db")
    assert not result.ok
    assert result.error.code == "db_not_found"


# ── DatabaseQueryTool tests ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_query_basic_select(tmp_path: Path):
    _create_db(tmp_path)
    tool = DatabaseQueryTool(workspace=tmp_path)
    result = await tool.execute(db_path="test.db", sql="SELECT * FROM users ORDER BY id")
    assert result.ok, result
    data = json.loads(result.content)
    assert data["count"] == 2
    assert data["rows"][0]["name"] == "Alice"


@pytest.mark.asyncio
async def test_query_blocks_write_sql(tmp_path: Path):
    _create_db(tmp_path)
    tool = DatabaseQueryTool(workspace=tmp_path)
    result = await tool.execute(db_path="test.db", sql="INSERT INTO users VALUES (3, 'Eve')")
    assert not result.ok
    assert result.error.code == "db_query_write_denied"


@pytest.mark.asyncio
async def test_query_cte_write_blocked_by_readonly_connection(tmp_path: Path):
    """CTE-wrapped write bypasses the keyword pre-check but is blocked by SQLite read-only URI mode."""
    _create_db(tmp_path)
    tool = DatabaseQueryTool(workspace=tmp_path)
    result = await tool.execute(
        db_path="test.db",
        sql="WITH x AS (DELETE FROM users) SELECT changes()",
    )
    assert not result.ok  # SQLite read-only mode rejects the DELETE inside the CTE


@pytest.mark.asyncio
async def test_query_rejects_path_outside_workspace(tmp_path: Path):
    tool = DatabaseQueryTool(workspace=tmp_path)
    result = await tool.execute(db_path="/etc/shadow.db", sql="SELECT 1")
    assert not result.ok
    assert result.error.code == "db_path_outside_workspace"


# ── DatabaseExecuteTool tests ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_basic_insert(tmp_path: Path):
    _create_db(tmp_path)
    tool = DatabaseExecuteTool(workspace=tmp_path)
    result = await tool.execute(db_path="test.db", sql="INSERT INTO users VALUES (3, 'Charlie')")
    assert result.ok, result
    data = json.loads(result.content)
    assert data["rowcount"] == 1


@pytest.mark.asyncio
async def test_execute_rollback_on_invalid_sql(tmp_path: Path):
    """Failed execute should rollback and leave original data intact."""
    _create_db(tmp_path)
    tool = DatabaseExecuteTool(workspace=tmp_path)
    result = await tool.execute(db_path="test.db", sql="INSERT INTO nonexistent_table VALUES (99)")
    assert not result.ok
    assert result.error.code == "db_execute_failed"
    # Verify the original two rows are still there (no partial commit)
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    rows = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    assert rows == 2


@pytest.mark.asyncio
async def test_execute_rejects_path_outside_workspace(tmp_path: Path):
    tool = DatabaseExecuteTool(workspace=tmp_path)
    result = await tool.execute(db_path="/etc/shadow.db", sql="INSERT INTO x VALUES (1)")
    assert not result.ok
    assert result.error.code == "db_path_outside_workspace"


# ── DatabaseInspectTool tests (continued) ────────────────────────────────────

@pytest.mark.asyncio
async def test_inspect_includes_indexes(tmp_path: Path):
    db = _create_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE UNIQUE INDEX idx_users_name ON users(name)")
    conn.commit()
    conn.close()

    tool = DatabaseInspectTool(workspace=tmp_path)
    result = await tool.execute(db_path="test.db")
    assert result.ok
    data = json.loads(result.content)
    users = next(t for t in data["tables"] if t["table"] == "users")
    idx_names = [i["name"] for i in users["indexes"]]
    assert "idx_users_name" in idx_names

"""Tests for forum scaffold database/service tools."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from zen_claw.agent.tools.database import (
    DatabaseExecuteTool,
    DatabaseQueryTool,
    DatabaseSchemaTool,
)
from zen_claw.agent.tools.result import ToolErrorKind


def _make_test_db(tmp_path: Path) -> Path:
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER)")
    conn.execute("INSERT INTO users VALUES (1, 'Alice', 30)")
    conn.execute("INSERT INTO users VALUES (2, 'Bob', 25)")
    conn.commit()
    conn.close()
    return db


def _sleep_command(tmp_path: Path, seconds: int = 15) -> str:
    script = tmp_path / "sleep_service.py"
    script.write_text(
        f"import time\n"
        f"time.sleep({seconds})\n",
        encoding="utf-8",
    )
    return f"{sys.executable} {script}"


async def test_db_query_select(tmp_path: Path):
    db = _make_test_db(tmp_path)
    tool = DatabaseQueryTool(workspace=tmp_path)
    result = await tool.execute(db_path=str(db), sql="SELECT * FROM users ORDER BY id")
    assert result.ok is True
    data = json.loads(result.content)
    assert data["count"] == 2
    assert data["rows"][0]["name"] == "Alice"


async def test_db_query_write_rejected(tmp_path: Path):
    db = _make_test_db(tmp_path)
    tool = DatabaseQueryTool(workspace=tmp_path)
    result = await tool.execute(db_path=str(db), sql="INSERT INTO users VALUES (3, 'Eve', 20)")
    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.PERMISSION
    assert result.error.code == "db_query_write_denied"


async def test_db_query_path_outside_workspace(tmp_path: Path):
    tool = DatabaseQueryTool(workspace=tmp_path)
    result = await tool.execute(db_path=str(Path("C:/outside/evil.db")), sql="SELECT 1")
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "db_path_outside_workspace"


async def test_db_execute_insert(tmp_path: Path):
    db = _make_test_db(tmp_path)
    tool = DatabaseExecuteTool(workspace=tmp_path)
    result = await tool.execute(db_path=str(db), sql="INSERT INTO users VALUES (3, 'Charlie', 22)")
    assert result.ok is True
    data = json.loads(result.content)
    assert data["rowcount"] == 1
    assert data["lastrowid"] == 3


async def test_db_schema_all_tables(tmp_path: Path):
    db = _make_test_db(tmp_path)
    tool = DatabaseSchemaTool(workspace=tmp_path)
    result = await tool.execute(db_path=str(db))
    assert result.ok is True
    schema = json.loads(result.content)
    assert "users" in schema
    assert {c["name"] for c in schema["users"]} >= {"id", "name", "age"}


async def test_db_schema_specific_table(tmp_path: Path):
    db = _make_test_db(tmp_path)
    tool = DatabaseSchemaTool(workspace=tmp_path)
    result = await tool.execute(db_path=str(db), table="users")
    assert result.ok is True
    schema = json.loads(result.content)
    assert list(schema.keys()) == ["users"]


async def test_db_query_not_found(tmp_path: Path):
    tool = DatabaseQueryTool(workspace=tmp_path)
    result = await tool.execute(db_path=str(tmp_path / "missing.db"), sql="SELECT 1")
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "db_not_found"


async def test_db_invalid_extension(tmp_path: Path):
    tool = DatabaseQueryTool(workspace=tmp_path)
    result = await tool.execute(db_path=str(tmp_path / "data.csv"), sql="SELECT 1")
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "db_invalid_extension"


async def test_service_start_and_status(tmp_path: Path):
    from zen_claw.agent.tools.service import ServiceStartTool, ServiceStatusTool, ServiceStopTool

    start_tool = ServiceStartTool(workspace=tmp_path)
    status_tool = ServiceStatusTool(workspace=tmp_path)
    stop_tool = ServiceStopTool(workspace=tmp_path)

    cmd = _sleep_command(tmp_path, 15)
    start = await start_tool.execute(name="test_service", command=cmd)
    assert start.ok is True
    start_data = json.loads(start.content)
    assert start_data["status"] == "started"
    assert int(start_data["pid"]) > 0

    status = await status_tool.execute(name="test_service")
    assert status.ok is True
    status_data = json.loads(status.content)
    assert status_data["running"] is True

    await stop_tool.execute(name="test_service")


async def test_service_start_already_running(tmp_path: Path):
    from zen_claw.agent.tools.service import ServiceStartTool, ServiceStopTool

    start_tool = ServiceStartTool(workspace=tmp_path)
    stop_tool = ServiceStopTool(workspace=tmp_path)
    cmd = _sleep_command(tmp_path, 15)

    r1 = await start_tool.execute(name="dup_service", command=cmd)
    assert r1.ok is True
    r2 = await start_tool.execute(name="dup_service", command=cmd)
    assert r2.ok is False
    assert r2.error is not None
    assert r2.error.code == "service_already_running"
    await stop_tool.execute(name="dup_service")


async def test_service_stop_not_found(tmp_path: Path):
    from zen_claw.agent.tools.service import ServiceStopTool

    tool = ServiceStopTool(workspace=tmp_path)
    result = await tool.execute(name="missing_service")
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "service_not_found"


async def test_service_status_all(tmp_path: Path):
    from zen_claw.agent.tools.service import ServiceStatusTool

    tool = ServiceStatusTool(workspace=tmp_path)
    result = await tool.execute()
    assert result.ok is True
    data = json.loads(result.content)
    assert "services" in data
    assert isinstance(data["services"], list)

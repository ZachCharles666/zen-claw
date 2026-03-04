"""SQLite database tools with workspace path restriction."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult


def _resolve_db_path(workspace: Path, db_path: str) -> Path | None:
    try:
        p = Path(db_path)
        if not p.is_absolute():
            p = (workspace / p).resolve()
        else:
            p = p.resolve()
        p.relative_to(workspace.resolve())
    except Exception:
        return None
    if p.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
        return Path("__invalid_ext__")
    return p


def _is_write_sql(sql: str) -> bool:
    first = str(sql or "").strip().split(" ", 1)[0].lower()
    return first in {"insert", "update", "delete", "create", "drop", "alter", "replace", "truncate", "pragma"}


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n //= 1024
    return f"{n:.1f} TB"


class DatabaseQueryTool(Tool):
    name = "database_query"
    description = "Run a read-only SQL SELECT query on workspace SQLite database."
    parameters = {
        "type": "object",
        "properties": {"db_path": {"type": "string"}, "sql": {"type": "string"}},
        "required": ["db_path", "sql"],
    }

    def __init__(self, workspace: Path):
        self._workspace = Path(workspace).resolve()

    async def execute(self, db_path: str, sql: str, **kwargs: Any) -> ToolResult:
        path = _resolve_db_path(self._workspace, db_path)
        if path is None:
            return ToolResult.failure(
                ToolErrorKind.PERMISSION, "db path must stay inside workspace", code="db_path_outside_workspace"
            )
        if str(path) == "__invalid_ext__":
            return ToolResult.failure(ToolErrorKind.PARAMETER, "db file must use .db/.sqlite extension", code="db_invalid_extension")
        if not path.exists():
            return ToolResult.failure(ToolErrorKind.PARAMETER, f"database not found: {path}", code="db_not_found")
        if _is_write_sql(sql):
            return ToolResult.failure(ToolErrorKind.PERMISSION, "write SQL denied in query tool", code="db_query_write_denied")
        try:
            # Open in SQLite read-only URI mode: enforces read-only at the engine level,
            # blocking CTE-wrapped writes (e.g. WITH x AS (DELETE ...) SELECT 1) that
            # bypass the keyword-based _is_write_sql() pre-check.
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute(sql)
                rows = [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()
        except Exception as exc:
            return ToolResult.failure(ToolErrorKind.RUNTIME, f"query failed: {exc}", code="db_query_failed")
        return ToolResult.success(json.dumps({"count": len(rows), "rows": rows}, ensure_ascii=False))


class DatabaseExecuteTool(Tool):
    name = "database_execute"
    description = "Run write SQL on workspace SQLite database."
    parameters = {
        "type": "object",
        "properties": {"db_path": {"type": "string"}, "sql": {"type": "string"}},
        "required": ["db_path", "sql"],
    }

    def __init__(self, workspace: Path):
        self._workspace = Path(workspace).resolve()

    async def execute(self, db_path: str, sql: str, **kwargs: Any) -> ToolResult:
        path = _resolve_db_path(self._workspace, db_path)
        if path is None:
            return ToolResult.failure(
                ToolErrorKind.PERMISSION, "db path must stay inside workspace", code="db_path_outside_workspace"
            )
        if str(path) == "__invalid_ext__":
            return ToolResult.failure(ToolErrorKind.PARAMETER, "db file must use .db/.sqlite extension", code="db_invalid_extension")
        if not path.exists():
            return ToolResult.failure(ToolErrorKind.PARAMETER, f"database not found: {path}", code="db_not_found")
        try:
            conn = sqlite3.connect(str(path))
            try:
                # `with conn:` auto-commits on success and auto-rolls back on any exception.
                with conn:
                    cur = conn.cursor()
                    cur.execute(sql)
                    rowcount = int(cur.rowcount or 0)
                    lastrowid = int(cur.lastrowid or 0)
            finally:
                conn.close()
        except Exception as exc:
            return ToolResult.failure(ToolErrorKind.RUNTIME, f"execute failed: {exc}", code="db_execute_failed")
        return ToolResult.success(json.dumps({"rowcount": rowcount, "lastrowid": lastrowid}, ensure_ascii=False))


class DatabaseSchemaTool(Tool):
    name = "database_schema"
    description = "Inspect SQLite schema in workspace database."
    parameters = {
        "type": "object",
        "properties": {"db_path": {"type": "string"}, "table": {"type": "string"}},
        "required": ["db_path"],
    }

    def __init__(self, workspace: Path):
        self._workspace = Path(workspace).resolve()

    async def execute(self, db_path: str, table: str | None = None, **kwargs: Any) -> ToolResult:
        path = _resolve_db_path(self._workspace, db_path)
        if path is None:
            return ToolResult.failure(
                ToolErrorKind.PERMISSION, "db path must stay inside workspace", code="db_path_outside_workspace"
            )
        if str(path) == "__invalid_ext__":
            return ToolResult.failure(ToolErrorKind.PARAMETER, "db file must use .db/.sqlite extension", code="db_invalid_extension")
        if not path.exists():
            return ToolResult.failure(ToolErrorKind.PARAMETER, f"database not found: {path}", code="db_not_found")
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                if table:
                    tables = [str(table)]
                else:
                    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
                    tables = [str(r["name"]) for r in cur.fetchall()]
                out: dict[str, list[dict[str, Any]]] = {}
                for t in tables:
                    cur.execute(f"PRAGMA table_info({t})")
                    cols = []
                    for r in cur.fetchall():
                        cols.append(
                            {
                                "cid": int(r["cid"]),
                                "name": str(r["name"]),
                                "type": str(r["type"] or ""),
                                "notnull": int(r["notnull"] or 0),
                                "default": r["dflt_value"],
                                "pk": int(r["pk"] or 0),
                            }
                        )
                    out[t] = cols
            finally:
                conn.close()
        except Exception as exc:
            return ToolResult.failure(ToolErrorKind.RUNTIME, f"schema inspect failed: {exc}", code="db_schema_failed")
        return ToolResult.success(json.dumps(out, ensure_ascii=False))


class DatabaseMigrateTool(Tool):
    """Run a DDL migration script atomically on a workspace SQLite DB.

    Statements are executed inside one transaction; the whole script rolls back if
    any statement fails. A migration-log table (``_nano_migrations``) prevents
    duplicate runs.
    """

    name = "database_migrate"
    description = (
        "Apply a DDL migration script to a workspace SQLite database atomically "
        "(rollback on error). Tracks executed migrations in _nano_migrations to "
        "prevent duplicates. Blocks DROP/TRUNCATE unless allow_destructive=true."
    )
    parameters = {
        "type": "object",
        "properties": {
            "db_path": {"type": "string", "description": "Relative path to the SQLite DB inside the workspace."},
            "migration_id": {
                "type": "string",
                "description": "Unique identifier (e.g. '001_create_posts'). Skipped if already applied.",
            },
            "sql": {
                "type": "string",
                "description": "One or more SQL DDL statements separated by semicolons.",
            },
            "allow_destructive": {
                "type": "boolean",
                "description": "Set to true to allow DROP TABLE / TRUNCATE. Defaults to false.",
            },
        },
        "required": ["db_path", "migration_id", "sql"],
    }

    _DESTRUCTIVE = {"drop", "truncate"}

    def __init__(self, workspace: Path):
        self._workspace = Path(workspace).resolve()

    async def execute(
        self,
        db_path: str,
        migration_id: str,
        sql: str,
        allow_destructive: bool = False,
        **kwargs: Any,
    ) -> ToolResult:
        path = _resolve_db_path(self._workspace, db_path)
        if path is None:
            return ToolResult.failure(ToolErrorKind.PERMISSION, "db path must stay inside workspace", code="db_path_outside_workspace")
        if str(path) == "__invalid_ext__":
            return ToolResult.failure(ToolErrorKind.PARAMETER, "db file must use .db/.sqlite extension", code="db_invalid_extension")

        mid = str(migration_id or "").strip()
        if not mid:
            return ToolResult.failure(ToolErrorKind.PARAMETER, "migration_id is required", code="db_migration_id_required")

        statements = [s.strip() for s in str(sql or "").split(";") if s.strip()]
        if not statements:
            return ToolResult.failure(ToolErrorKind.PARAMETER, "no SQL statements provided", code="db_migration_empty")

        if not allow_destructive:
            for stmt in statements:
                parts = stmt.strip().split()
                first_word = parts[0].lower() if parts else ""
                if first_word in self._DESTRUCTIVE:
                    return ToolResult.failure(
                        ToolErrorKind.PERMISSION,
                        f"Statement '{first_word.upper()}' is destructive; set allow_destructive=true to proceed.",
                        code="db_migration_destructive_denied",
                    )

        try:
            from datetime import UTC, datetime
            conn = sqlite3.connect(str(path))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS _nano_migrations "
                "(id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            conn.commit()

            existing = conn.execute("SELECT id FROM _nano_migrations WHERE id = ?", (mid,)).fetchone()
            if existing:
                conn.close()
                return ToolResult.success(
                    json.dumps({"migration_id": mid, "status": "skipped", "reason": "already_applied"}, ensure_ascii=False)
                )

            try:
                conn.execute("BEGIN")
                for stmt in statements:
                    conn.execute(stmt)
                conn.execute(
                    "INSERT INTO _nano_migrations (id, applied_at) VALUES (?, ?)",
                    (mid, datetime.now(UTC).isoformat()),
                )
                conn.execute("COMMIT")
            except Exception as exc:
                conn.execute("ROLLBACK")
                conn.close()
                return ToolResult.failure(ToolErrorKind.RUNTIME, f"migration failed (rolled back): {exc}", code="db_migration_failed")

            conn.close()
        except Exception as exc:
            return ToolResult.failure(ToolErrorKind.RUNTIME, f"database error: {exc}", code="db_migration_error")

        return ToolResult.success(
            json.dumps(
                {"migration_id": mid, "status": "applied", "statements_run": len(statements)},
                ensure_ascii=False,
            )
        )


class DatabaseInspectTool(Tool):
    """Return a rich summary of a workspace SQLite database.

    Reports table list, row counts, column counts, index names, and estimated size.
    """

    name = "database_inspect"
    description = (
        "Inspect a workspace SQLite database: list all tables with row counts, "
        "column counts, indexes, and estimated DB size."
    )
    parameters = {
        "type": "object",
        "properties": {
            "db_path": {"type": "string", "description": "Relative path to the SQLite DB inside the workspace."},
        },
        "required": ["db_path"],
    }

    def __init__(self, workspace: Path):
        self._workspace = Path(workspace).resolve()

    async def execute(self, db_path: str, **kwargs: Any) -> ToolResult:
        path = _resolve_db_path(self._workspace, db_path)
        if path is None:
            return ToolResult.failure(ToolErrorKind.PERMISSION, "db path must stay inside workspace", code="db_path_outside_workspace")
        if str(path) == "__invalid_ext__":
            return ToolResult.failure(ToolErrorKind.PARAMETER, "db file must use .db/.sqlite extension", code="db_invalid_extension")
        if not path.exists():
            return ToolResult.failure(ToolErrorKind.PARAMETER, f"database not found: {path}", code="db_not_found")

        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                conn.row_factory = sqlite3.Row

                page_count = conn.execute("PRAGMA page_count").fetchone()[0]
                page_size = conn.execute("PRAGMA page_size").fetchone()[0]
                size_bytes = int(page_count) * int(page_size)

                tables_raw = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
                tables = [str(r["name"]) for r in tables_raw]

                table_info: list[dict[str, Any]] = []
                for t in tables:
                    try:
                        row_count = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                    except Exception:
                        row_count = -1
                    cols = conn.execute(f'PRAGMA table_info("{t}")').fetchall()
                    indexes = conn.execute(f'PRAGMA index_list("{t}")').fetchall()
                    table_info.append(
                        {
                            "table": t,
                            "row_count": int(row_count),
                            "column_count": len(cols),
                            "columns": [str(c["name"]) for c in cols],
                            "indexes": [
                                {"name": str(r["name"]), "unique": bool(r["unique"])} for r in indexes
                            ],
                        }
                    )
            finally:
                conn.close()
        except Exception as exc:
            return ToolResult.failure(ToolErrorKind.RUNTIME, f"inspect failed: {exc}", code="db_inspect_failed")

        return ToolResult.success(
            json.dumps(
                {
                    "db_path": str(path.relative_to(self._workspace)),
                    "size_bytes": size_bytes,
                    "size_human": _human_bytes(size_bytes),
                    "table_count": len(tables),
                    "tables": table_info,
                },
                ensure_ascii=False,
            )
        )

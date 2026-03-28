"""Tests for the AuditWorker JSONL audit logging."""

import asyncio
import json

from zen_claw.observability.audit import AuditWorker

# ---------------------------------------------------------------------------
# AuditWorker unit tests
# ---------------------------------------------------------------------------


def test_audit_creates_jsonl_file(tmp_path):
    """audit_turn writes a JSONL file under data_dir/audit_logs/."""
    worker = AuditWorker(data_dir=tmp_path)
    asyncio.run(
        worker.audit_turn("trace-001", {"event_type": "tool.executed", "tool": "web_fetch", "ok": True})
    )
    log_files = list((tmp_path / "audit_logs").glob("*.jsonl"))
    assert len(log_files) == 1


def test_audit_record_fields(tmp_path):
    """Each record contains ts, trace_id, event_type, and extra fields."""
    worker = AuditWorker(data_dir=tmp_path)
    asyncio.run(
        worker.audit_turn(
            "trace-abc",
            {"event_type": "message.inbound", "channel": "slack", "chat_id": "C123"},
        )
    )
    log_file = next((tmp_path / "audit_logs").glob("*.jsonl"))
    record = json.loads(log_file.read_text(encoding="utf-8").strip())

    assert record["trace_id"] == "trace-abc"
    assert record["event_type"] == "message.inbound"
    assert record["channel"] == "slack"
    assert record["chat_id"] == "C123"
    assert "ts" in record


def test_audit_multiple_records_appended(tmp_path):
    """Multiple calls append multiple lines to the same day file."""
    worker = AuditWorker(data_dir=tmp_path)
    asyncio.run(worker.audit_turn("t1", {"event_type": "tool.executed", "tool": "exec", "ok": True}))
    asyncio.run(worker.audit_turn("t2", {"event_type": "tool.executed", "tool": "web_fetch", "ok": False}))

    log_file = next((tmp_path / "audit_logs").glob("*.jsonl"))
    lines = [line for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["trace_id"] == "t1"
    assert json.loads(lines[1])["trace_id"] == "t2"


def test_audit_no_data_dir_returns_true():
    """Without data_dir, audit_turn returns True (no-op)."""
    worker = AuditWorker(data_dir=None)
    result = asyncio.run(worker.audit_turn("t", {"event_type": "tool.executed"}))
    assert result is True


def test_audit_no_data_dir_creates_no_files(tmp_path):
    """Without data_dir, no files are created."""
    worker = AuditWorker(data_dir=None)
    asyncio.run(worker.audit_turn("t", {"event_type": "tool.executed"}))
    assert not list(tmp_path.glob("**/*.jsonl"))


# ---------------------------------------------------------------------------
# Integration: tool execution produces audit record
# ---------------------------------------------------------------------------


def test_tool_execution_audit(tmp_path):
    """ToolRegistry emits a tool.executed audit record on successful tool call."""

    from zen_claw.agent.tools.base import Tool
    from zen_claw.agent.tools.registry import ToolRegistry
    from zen_claw.agent.tools.result import ToolResult

    class _EchoTool(Tool):
        name = "echo"
        description = "echo tool"
        parameters = {}

        async def execute(self, msg: str = "", trace_id: str | None = None) -> ToolResult:
            return ToolResult.success(msg)

    worker = AuditWorker(data_dir=tmp_path)
    registry = ToolRegistry(audit_worker=worker)
    registry.register(_EchoTool())

    asyncio.run(registry.execute("echo", {"msg": "hello"}, trace_id="trace-xyz"))

    log_file = next((tmp_path / "audit_logs").glob("*.jsonl"))
    record = json.loads(log_file.read_text(encoding="utf-8").strip())
    assert record["event_type"] == "tool.executed"
    assert record["tool"] == "echo"
    assert record["ok"] is True
    assert record["trace_id"] == "trace-xyz"


def test_tool_execution_no_audit_without_trace_id(tmp_path):
    """ToolRegistry skips audit when trace_id is None."""
    from zen_claw.agent.tools.base import Tool
    from zen_claw.agent.tools.registry import ToolRegistry
    from zen_claw.agent.tools.result import ToolResult

    class _EchoTool(Tool):
        name = "echo"
        description = "echo"
        parameters = {}

        async def execute(self, trace_id: str | None = None) -> ToolResult:
            return ToolResult.success("ok")

    worker = AuditWorker(data_dir=tmp_path)
    registry = ToolRegistry(audit_worker=worker)
    registry.register(_EchoTool())

    asyncio.run(registry.execute("echo", {}, trace_id=None))

    log_files = list((tmp_path / "audit_logs").glob("*.jsonl"))
    assert len(log_files) == 0

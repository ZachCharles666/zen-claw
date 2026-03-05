from pathlib import Path
from typing import Any

from zen_claw.agent.tools.cron import CronTool
from zen_claw.agent.tools.filesystem import ReadFileTool
from zen_claw.agent.tools.message import MessageTool
from zen_claw.agent.tools.result import ToolErrorKind
from zen_claw.agent.tools.shell import ExecTool
from zen_claw.agent.tools.spawn import SpawnTool
from zen_claw.agent.tools.web import WebFetchTool, WebSearchTool
from zen_claw.bus.events import OutboundMessage
from zen_claw.cron.service import CronService


async def test_exec_tool_returns_structured_guard_error() -> None:
    tool = ExecTool(restrict_to_workspace=True, working_dir=str(Path.cwd()))
    result = await tool.execute("rm -rf /")
    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.PERMISSION
    assert result.error.code == "exec_guard_dangerous_pattern"


async def test_filesystem_tool_returns_structured_error(tmp_path: Path) -> None:
    tool = ReadFileTool(allowed_dir=tmp_path)
    result = await tool.execute(str(tmp_path / "missing.txt"))
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "file_not_found"


async def test_web_tools_return_structured_errors_for_invalid_inputs() -> None:
    search = WebSearchTool(api_key="")
    result = await search.execute("hello")
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "brave_api_key_missing"

    fetch = WebFetchTool()
    bad = await fetch.execute("ftp://example.com")
    assert bad.ok is False
    assert bad.error is not None
    assert bad.error.code == "url_invalid"


async def test_message_tool_returns_structured_error_without_context() -> None:
    tool = MessageTool(send_callback=None)
    result = await tool.execute("hi")
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "message_target_missing"


async def test_cron_tool_returns_structured_error_for_unknown_action(tmp_path: Path) -> None:
    service = CronService(tmp_path / "jobs.json")
    tool = CronTool(service)
    result = await tool.execute(action="unknown")
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "cron_unknown_action"


class _FakeManager:
    async def spawn(self, **kwargs: Any) -> str:
        return "spawned"


async def test_spawn_tool_returns_structured_success() -> None:
    tool = SpawnTool(manager=_FakeManager())
    result = await tool.execute(task="do work")
    assert result.ok is True
    assert "spawned" in result.content


async def test_message_tool_returns_structured_success_with_callback() -> None:
    sent: list[OutboundMessage] = []

    async def cb(msg: OutboundMessage) -> None:
        sent.append(msg)

    tool = MessageTool(send_callback=cb, default_channel="cli", default_chat_id="direct")
    result = await tool.execute("hello")
    assert result.ok is True
    assert sent and sent[0].content == "hello"

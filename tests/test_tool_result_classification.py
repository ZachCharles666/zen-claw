from pathlib import Path
from typing import Any

from zen_claw.agent.context import ContextBuilder
from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.registry import ToolRegistry
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult


class _LegacyErrorTool(Tool):
    def __init__(self, response: str):
        self._response = response

    @property
    def name(self) -> str:
        return "legacy"

    @property
    def description(self) -> str:
        return "legacy result tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        return self._response


class _StructuredTool(Tool):
    @property
    def name(self) -> str:
        return "structured"

    @property
    def description(self) -> str:
        return "structured"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult.failure(ToolErrorKind.PERMISSION, "blocked")


async def test_registry_classifies_retryable_error_from_legacy_text() -> None:
    reg = ToolRegistry()
    reg.register(_LegacyErrorTool("Error: Command timed out after 10 seconds"))
    result = await reg.execute("legacy", {})
    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.RETRYABLE
    assert result.error.retryable is True


async def test_registry_classifies_permission_error_from_legacy_text() -> None:
    reg = ToolRegistry()
    reg.register(_LegacyErrorTool("Error: Command blocked by safety guard"))
    result = await reg.execute("legacy", {})
    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.PERMISSION


async def test_registry_passes_structured_tool_result() -> None:
    reg = ToolRegistry()
    reg.register(_StructuredTool())
    result = await reg.execute("structured", {})
    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.PERMISSION


def test_context_serializes_tool_error_result() -> None:
    ctx = ContextBuilder(workspace=Path("."))
    messages: list[dict[str, Any]] = []
    result = ToolResult.failure(ToolErrorKind.PARAMETER, "missing query")
    out = ctx.add_tool_result(messages, "tc-1", "demo", result)
    content = out[-1]["content"]
    assert isinstance(content, str)
    assert content.startswith("[tool_error]")



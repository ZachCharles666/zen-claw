from typing import Any

from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.registry import ToolRegistry


class _TraceEchoTool(Tool):
    @property
    def name(self) -> str:
        return "trace_echo"

    @property
    def description(self) -> str:
        return "trace echo"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        return str(kwargs.get("trace_id") or "")


async def test_registry_passes_trace_id_to_tool_execute() -> None:
    reg = ToolRegistry()
    reg.register(_TraceEchoTool())
    result = await reg.execute("trace_echo", {}, trace_id="trace-123")
    assert result.ok is True
    assert result.content == "trace-123"

"""Spawn tool for creating background subagents."""

from typing import TYPE_CHECKING, Any

from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult

if TYPE_CHECKING:
    from zen_claw.agent.subagent import SubagentManager


class SpawnTool(Tool):
    """
    Tool to spawn a subagent for background task execution.

    The subagent runs asynchronously and announces its result back
    to the main agent when complete.
    """

    def __init__(self, manager: "SubagentManager"):
        self._manager = manager
        self._origin_channel = "cli"
        self._origin_chat_id = "direct"
        self._trace_id: str | None = None
        self._skill_pins: dict[str, str] | None = None

    def set_context(
        self,
        channel: str,
        chat_id: str,
        trace_id: str | None = None,
        skill_pins: dict[str, str] | None = None,
    ) -> None:
        """Set the origin context and skill version pins for subagent announcements."""
        self._origin_channel = channel
        self._origin_chat_id = chat_id
        self._trace_id = trace_id
        self._skill_pins = skill_pins

    @property
    def name(self) -> str:
        return "spawn"

    @property
    def description(self) -> str:
        return (
            "Spawn a subagent to handle a task in the background. "
            "Use this for complex or time-consuming tasks that can run independently. "
            "The subagent will complete the task and report back when done."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task for the subagent to complete",
                },
                "label": {
                    "type": "string",
                    "description": "Optional short label for the task (for display)",
                },
            },
            "required": ["task"],
        }

    async def execute(self, task: str, label: str | None = None, **kwargs: Any) -> ToolResult:
        """Spawn a subagent to execute the given task."""
        try:
            msg = await self._manager.spawn(
                task=task,
                label=label,
                origin_channel=self._origin_channel,
                origin_chat_id=self._origin_chat_id,
                parent_trace_id=self._trace_id or kwargs.get("trace_id"),
                skill_pins=self._skill_pins,
            )
            return ToolResult.success(msg)
        except Exception as e:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                f"Failed to spawn subagent: {str(e)}",
                code="spawn_failed",
            )

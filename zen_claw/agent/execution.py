"""Execution controller for plan -> execute -> reflect loop."""

import json
from dataclasses import dataclass

from zen_claw.agent.tools.result import ToolErrorKind, ToolResult


@dataclass
class ReflectionHint:
    """Summarized error signal for reflection step."""

    kind: str
    message: str
    retryable: bool


class ExecutionController:
    """Minimal controller for planning and reflection-guided retries."""

    def __init__(self, max_reflections: int = 1, enable_planning: bool = True):
        self.max_reflections = max_reflections
        self.enable_planning = enable_planning

    def should_plan(self) -> bool:
        return self.enable_planning

    def build_plan_prompt(self, user_goal: str) -> str:
        return (
            "Before using tools, produce a concise execution plan (max 4 bullets) for this request. "
            "Do not call tools in this step.\n\n"
            f"User goal:\n{user_goal}"
        )

    def can_reflect(self, used_reflections: int) -> bool:
        return used_reflections < self.max_reflections

    def collect_error_hints(self, tool_results: list[ToolResult]) -> list[ReflectionHint]:
        hints: list[ReflectionHint] = []
        for r in tool_results:
            if r.ok or not r.error:
                continue
            hints.append(
                ReflectionHint(
                    kind=r.error.kind.value,
                    message=r.error.message[:200],
                    retryable=r.error.retryable,
                )
            )
        return hints

    def build_reflection_prompt(self, hints: list[ReflectionHint]) -> str:
        lines = []
        for i, h in enumerate(hints[:4], 1):
            lines.append(
                f"{i}. kind={h.kind}, retryable={str(h.retryable).lower()}, error={h.message}"
            )
        joined = "\n".join(lines) if lines else "no tool error details"
        return (
            "Previous tool attempts failed. Reflect and adjust your strategy.\n"
            "Rules:\n"
            "- If retryable=true, retry with safer/smaller scope.\n"
            "- If kind=parameter, fix arguments.\n"
            "- If kind=permission, choose an allowed alternative.\n"
            "- If kind=runtime, switch approach or degrade gracefully.\n\n"
            "Observed errors:\n"
            f"{joined}"
        )

    def parse_tool_error_content(self, content: str) -> ReflectionHint | None:
        prefix = "[tool_error] "
        if not content.startswith(prefix):
            return None
        try:
            payload = json.loads(content[len(prefix) :])
            err = payload.get("error", {})
            kind = str(err.get("kind", ToolErrorKind.RUNTIME.value))
            message = str(err.get("message", ""))
            retryable = bool(err.get("retryable", False))
            return ReflectionHint(kind=kind, message=message, retryable=retryable)
        except Exception:
            return None

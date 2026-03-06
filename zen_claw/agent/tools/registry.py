"""Tool registry for dynamic tool management."""

import json
import time
from typing import Any

from loguru import logger

from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.policy import ToolPolicyEngine
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult
from zen_claw.observability.trace import TraceContext


class ToolRegistry:
    """
    Registry for agent tools.

    Allows dynamic registration and execution of tools.
    """

    def __init__(self, policy: ToolPolicyEngine | None = None, quota_engine: Any = None):
        self._tools: dict[str, Tool] = {}
        self._policy = policy or ToolPolicyEngine()
        self._quota_engine = quota_engine
        self._kill_switch_enabled: bool = False
        self._kill_switch_reason: str = ""
        # Optional additional gate (e.g. when a specific skill/plugin is loaded).
        # When set, tools not in this allow-list are denied regardless of policy config.
        self._skill_allowed_tools: set[str] | None = None
        self._active_skill_names: list[str] = []
        self._skill_permissions_mode: str = "off"

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get all tool definitions in OpenAI format."""
        return [tool.to_schema() for tool in self._tools.values()]

    def get_visible_definitions(
        self,
        *,
        extra_allow: set[str] | list[str] | tuple[str, ...] | None = None,
        extra_deny: set[str] | list[str] | tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """Get only tool definitions visible under current/runtime-local constraints."""
        allow_set = (
            {str(t).strip().lower() for t in extra_allow if str(t).strip()}
            if extra_allow is not None
            else None
        )
        deny_set = (
            {str(t).strip().lower() for t in extra_deny if str(t).strip()}
            if extra_deny is not None
            else set()
        )

        visible: list[dict[str, Any]] = []
        for name, tool in self._tools.items():
            token = str(name or "").strip().lower()
            if token in deny_set or "*" in deny_set:
                continue
            if allow_set is not None and "*" not in allow_set and token not in allow_set:
                continue
            if not self._is_visible_under_current_policy(token):
                continue
            visible.append(tool.to_schema())
        return visible

    def set_policy_scope(
        self,
        scope: str,
        *,
        allow: set[str] | list[str] | tuple[str, ...] | None = None,
        deny: set[str] | list[str] | tuple[str, ...] | None = None,
    ) -> None:
        """Set tool policy for a scope."""
        self._policy.set_scope(scope, allow=allow, deny=deny)

    def clear_policy_scope(self, scope: str) -> None:
        """Clear tool policy for a scope."""
        self._policy.clear_scope(scope)

    async def execute(
        self,
        name: str,
        params: dict[str, Any],
        trace_id: str | None = None,
    ) -> ToolResult:
        """
        Execute a tool by name with given parameters.

        Args:
            name: Tool name.
            params: Tool parameters.

        Returns:
            Tool execution result as string.

        Raises:
            KeyError: If tool not found.
        """
        started = time.perf_counter()
        tool = self._tools.get(name)
        if not tool:
            result = ToolResult.failure(
                ToolErrorKind.PARAMETER,
                f"Tool '{name}' not found",
                code="tool_not_found",
            )
            logger.warning(
                "Tool not found "
                + TraceContext.event_text(
                    "tool.lookup",
                    trace_id,
                    tool=name,
                    error_kind=result.error.kind.value if result.error else None,
                    retryable=result.error.retryable if result.error else None,
                    skill_names=self._active_skill_names or None,
                    skill_permissions_mode=self._skill_permissions_mode,
                )
            )
            return result

        if self._kill_switch_enabled:
            suffix = f" ({self._kill_switch_reason})" if self._kill_switch_reason else ""
            result = ToolResult.failure(
                ToolErrorKind.PERMISSION,
                f"Tool automation is disabled by global kill switch{suffix}",
                code="tool_kill_switch_enabled",
                policy_scope="global_kill_switch",
                skill_names=self._active_skill_names,
                skill_permissions_mode=self._skill_permissions_mode,
            )
            logger.warning(
                "Tool blocked by kill switch "
                + TraceContext.event_text(
                    "tool.killswitch.denied",
                    trace_id,
                    tool=name,
                    policy_scope="global_kill_switch",
                    policy_code="tool_kill_switch_enabled",
                    error_kind=result.error.kind.value if result.error else None,
                    retryable=result.error.retryable if result.error else None,
                    kill_switch_reason=self._kill_switch_reason or None,
                    skill_names=self._active_skill_names or None,
                    skill_permissions_mode=self._skill_permissions_mode,
                )
            )
            return result

        if self._skill_allowed_tools is not None:
            t = (name or "").strip().lower()
            if "*" not in self._skill_allowed_tools and t not in self._skill_allowed_tools:
                result = ToolResult.failure(
                    ToolErrorKind.PERMISSION,
                    f"Tool '{t}' is not permitted by active skill permission gate",
                    code="skill_permission_denied",
                    policy_scope="skill",
                    skill_names=self._active_skill_names,
                    skill_permissions_mode=self._skill_permissions_mode,
                )
                logger.warning(
                    "Tool blocked by skill permission gate "
                    + TraceContext.event_text(
                        "tool.skill.denied",
                        trace_id,
                        tool=name,
                        policy_scope="skill",
                        policy_code="skill_permission_denied",
                        error_kind=result.error.kind.value if result.error else None,
                        retryable=result.error.retryable if result.error else None,
                        skill_names=self._active_skill_names or None,
                        skill_permissions_mode=self._skill_permissions_mode,
                    )
                )
                return result

        decision = self._policy.evaluate(name)
        if not decision.allowed:
            result = ToolResult.failure(
                ToolErrorKind.PERMISSION,
                decision.reason,
                code=decision.code,
                policy_scope=decision.scope or "",
                skill_names=self._active_skill_names,
                skill_permissions_mode=self._skill_permissions_mode,
            )
            logger.warning(
                "Tool blocked by policy "
                + TraceContext.event_text(
                    "tool.policy.denied",
                    trace_id,
                    tool=name,
                    policy_scope=decision.scope,
                    policy_code=decision.code,
                    error_kind=result.error.kind.value if result.error else None,
                    retryable=result.error.retryable if result.error else None,
                    skill_names=self._active_skill_names or None,
                    skill_permissions_mode=self._skill_permissions_mode,
                )
            )
            return result

        # Check Quota
        if self._quota_engine:
            tenant_id = trace_id.split(":")[0] if trace_id and ":" in trace_id else "default"
            quota_ok = await self._quota_engine.check_quota(tenant_id, name)
            if not quota_ok:
                result = ToolResult.failure(
                    ToolErrorKind.RETRYABLE,
                    f"Quota exceeded for tool '{name}' (or quota service unavailable)",
                    code="quota_exceeded",
                )
                logger.warning(f"Tool '{name}' denied by quota engine for tenant '{tenant_id}'")
                return result

        try:
            errors = tool.validate_params(params)
            if errors:
                message = "; ".join(errors)
                result = ToolResult.failure(
                    ToolErrorKind.PARAMETER,
                    message,
                    code="invalid_parameters",
                )
                logger.warning(
                    "Tool params invalid "
                    + TraceContext.event_text(
                        "tool.validate",
                        trace_id,
                        tool=name,
                        errors=errors,
                        error_kind=result.error.kind.value if result.error else None,
                        retryable=result.error.retryable if result.error else None,
                        skill_names=self._active_skill_names or None,
                        skill_permissions_mode=self._skill_permissions_mode,
                    )
                )
                return result

            raw_result = await tool.execute(**params, trace_id=trace_id)
            result = self._normalize_result(name, raw_result)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                "Tool executed "
                + TraceContext.event_text(
                    "tool.done",
                    trace_id,
                    tool=name,
                    elapsed_ms=elapsed_ms,
                    ok=result.ok,
                    error_kind=result.error.kind.value if result.error else None,
                    retryable=result.error.retryable if result.error else None,
                    skill_names=self._active_skill_names or None,
                    skill_permissions_mode=self._skill_permissions_mode,
                )
            )
            return result
        except PermissionError as e:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            result = ToolResult.failure(
                ToolErrorKind.PERMISSION,
                str(e),
                code="permission_denied",
                skill_names=self._active_skill_names,
                skill_permissions_mode=self._skill_permissions_mode,
            )
            logger.error(
                "Tool execution failed "
                + TraceContext.event_text(
                    "tool.error",
                    trace_id,
                    tool=name,
                    elapsed_ms=elapsed_ms,
                    error=str(e),
                    error_kind=result.error.kind.value,
                    retryable=result.error.retryable,
                    skill_names=self._active_skill_names or None,
                    skill_permissions_mode=self._skill_permissions_mode,
                )
            )
            return result
        except Exception as e:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            result = ToolResult.failure(
                ToolErrorKind.RUNTIME,
                str(e),
                code="tool_exception",
                skill_names=self._active_skill_names,
                skill_permissions_mode=self._skill_permissions_mode,
            )
            logger.error(
                "Tool execution failed "
                + TraceContext.event_text(
                    "tool.error",
                    trace_id,
                    tool=name,
                    elapsed_ms=elapsed_ms,
                    error=str(e),
                    error_kind=result.error.kind.value,
                    retryable=result.error.retryable,
                    skill_names=self._active_skill_names or None,
                    skill_permissions_mode=self._skill_permissions_mode,
                )
            )
            return result

    def _normalize_result(self, name: str, raw_result: Any) -> ToolResult:
        """Normalize raw tool output into ToolResult."""
        if isinstance(raw_result, ToolResult):
            result = raw_result
        elif isinstance(raw_result, str):
            result = self._from_legacy_string(name, raw_result)
        else:
            result = ToolResult.success(str(raw_result))

        return result.purify()

    def _is_visible_under_current_policy(self, name: str) -> bool:
        if self._kill_switch_enabled:
            return False
        if self._skill_allowed_tools is not None:
            if "*" not in self._skill_allowed_tools and name not in self._skill_allowed_tools:
                return False
        decision = self._policy.evaluate(name)
        return bool(decision.allowed)

    def _from_legacy_string(self, name: str, text: str) -> ToolResult:
        stripped = text.strip()
        lower = stripped.lower()

        # Legacy success path
        if not lower.startswith("error"):
            if lower.startswith("{") and '"error"' in lower:
                parsed = self._try_parse_json_error(stripped)
                if parsed:
                    kind, message, code = parsed
                    return ToolResult.failure(kind, message, code=code)
            return ToolResult.success(text)

        message = stripped.split(":", 1)[1].strip() if ":" in stripped else stripped
        kind = self._classify_error_kind(name=name, message=message)
        return ToolResult.failure(kind, message, code="legacy_error")

    def _try_parse_json_error(self, text: str) -> tuple[ToolErrorKind, str, str] | None:
        try:
            data = json.loads(text)
        except Exception:
            return None

        err = data.get("error")
        if not err:
            return None
        message = str(err)
        return self._classify_error_kind(name="", message=message), message, "json_error"

    def _classify_error_kind(self, name: str, message: str) -> ToolErrorKind:
        msg = message.lower()
        tool = name.lower()

        parameter_keywords = [
            "invalid parameter",
            "missing required",
            "required",
            "not found",
            "unknown action",
            "validation failed",
            "url validation failed",
            "old_text not found",
            "not a file",
            "not a directory",
        ]
        permission_keywords = [
            "permission",
            "outside allowed directory",
            "blocked by safety guard",
            "path traversal",
            "allowlist",
            "forbidden",
            "denied",
        ]
        retryable_keywords = [
            "timed out",
            "timeout",
            "temporarily",
            "connection reset",
            "connection refused",
            "network",
            "rate limit",
            "429",
            "service unavailable",
            "try again",
        ]

        if any(k in msg for k in permission_keywords):
            return ToolErrorKind.PERMISSION
        if any(k in msg for k in retryable_keywords):
            return ToolErrorKind.RETRYABLE
        if any(k in msg for k in parameter_keywords):
            return ToolErrorKind.PARAMETER

        if tool in {"web_search", "web_fetch"} and "http" in msg:
            return ToolErrorKind.RETRYABLE

        return ToolErrorKind.RUNTIME

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys())

    def set_skill_allowed_tools(self, allow: set[str] | list[str] | tuple[str, ...]) -> None:
        """Set/replace the skill permission gate allow-list."""
        self._skill_allowed_tools = {str(t).strip().lower() for t in allow if str(t).strip()}

    def clear_skill_allowed_tools(self) -> None:
        """Disable the skill permission gate."""
        self._skill_allowed_tools = None

    def set_skill_attribution(self, skill_names: list[str] | None, mode: str = "off") -> None:
        """Set active skill attribution context for audit/log correlation."""
        names: list[str] = []
        seen: set[str] = set()
        for n in skill_names or []:
            token = str(n or "").strip()
            if not token:
                continue
            if token in seen:
                continue
            seen.add(token)
            names.append(token)
        self._active_skill_names = names
        self._skill_permissions_mode = (mode or "off").strip().lower()

    def set_kill_switch(self, enabled: bool, reason: str | None = None) -> None:
        """Enable/disable the global tool execution kill switch."""
        self._kill_switch_enabled = bool(enabled)
        self._kill_switch_reason = (reason or "").strip()

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

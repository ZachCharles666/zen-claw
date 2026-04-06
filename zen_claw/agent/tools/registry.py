"""Tool registry for dynamic tool management."""

import inspect
import json
import time
from typing import Any

from loguru import logger

from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.capability_policy import (
    CapabilityPolicyEvaluator,
    capability_request_from_tool,
)
from zen_claw.agent.tools.policy import ToolPolicyEngine
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult
from zen_claw.observability.audit import AuditWorker
from zen_claw.observability.trace import TraceContext
from zen_claw.security_context import (
    ensure_security_envelope,
    extract_security_context,
    verify_gateway_envelope,
)


class ToolRegistry:
    """
    Registry for agent tools.

    Allows dynamic registration and execution of tools.
    """

    def __init__(
        self,
        policy: ToolPolicyEngine | None = None,
        quota_engine: Any = None,
        audit_worker: AuditWorker | None = None,
    ):
        self._tools: dict[str, Tool] = {}
        self._policy = policy or ToolPolicyEngine()
        self._quota_engine = quota_engine
        self._audit_worker = audit_worker
        self._kill_switch_enabled: bool = False
        self._kill_switch_reason: str = ""
        # Optional additional gate (e.g. when a specific skill/plugin is loaded).
        # When set, tools not in this allow-list are denied regardless of policy config.
        self._skill_allowed_tools: set[str] | None = None
        self._profile_allowed_tools: set[str] | None = None
        self._profile_denied_tools: set[str] = set()
        self._active_skill_names: list[str] = []
        self._skill_permissions_mode: str = "off"
        self._security_policy: Any = None
        self._request_context: dict[str, Any] = {}

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
        query_hint: str | None = None,
        semantic_top_k: int = 10,
        semantic_threshold: int = 12,
    ) -> list[dict[str, Any]]:
        """Get only tool definitions visible under current/runtime-local constraints.

        Parameters
        ----------
        query_hint:
            When provided and the visible tool count exceeds *semantic_threshold*,
            BM25 relevance scoring is applied and only the top *semantic_top_k*
            most relevant tools are returned.  This reduces input-token usage on
            agents with many registered tools.  ``None`` preserves the original
            behaviour (no semantic filtering).
        semantic_top_k:
            Maximum number of tools to keep after BM25 filtering.
        semantic_threshold:
            Minimum number of visible tools required before BM25 filtering is
            applied.  Below this number the full set is always returned.
        """
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

        # C2 — semantic tool selection via BM25 when query_hint is provided
        if query_hint and len(visible) > semantic_threshold:
            from zen_claw.agent.tools.semantic_selector import bm25_select
            visible = bm25_select(visible, query_hint, top_k=semantic_top_k)

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

    def set_security_policy(self, policy: Any) -> None:
        """Attach the current security policy config for resource-scoped checks."""
        self._security_policy = policy

    def set_request_context(self, context: dict[str, Any] | None) -> None:
        """Attach per-request identity/policy context for resource checks and audit."""
        self._request_context = ensure_security_envelope(dict(context or {}))

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
        t = (name or "").strip().lower()
        if t in self._profile_denied_tools or "*" in self._profile_denied_tools:
            result = ToolResult.failure(
                ToolErrorKind.PERMISSION,
                f"Tool '{t}' is denied by active agent profile binding",
                code="profile_tool_denied",
                policy_scope="agent_profile",
                skill_names=self._active_skill_names,
                skill_permissions_mode=self._skill_permissions_mode,
            )
            logger.warning(
                "Tool blocked by profile deny gate "
                + TraceContext.event_text(
                    "tool.profile.denied",
                    trace_id,
                    tool=name,
                    policy_scope="agent_profile",
                    policy_code="profile_tool_denied",
                    error_kind=result.error.kind.value if result.error else None,
                    retryable=result.error.retryable if result.error else None,
                    skill_names=self._active_skill_names or None,
                    skill_permissions_mode=self._skill_permissions_mode,
                )
            )
            return result
        if self._profile_allowed_tools is not None:
            if "*" not in self._profile_allowed_tools and t not in self._profile_allowed_tools:
                result = ToolResult.failure(
                    ToolErrorKind.PERMISSION,
                    f"Tool '{t}' is not permitted by active agent profile binding",
                    code="profile_tool_not_allowlisted",
                    policy_scope="agent_profile",
                    skill_names=self._active_skill_names,
                    skill_permissions_mode=self._skill_permissions_mode,
                )
                logger.warning(
                    "Tool blocked by profile allow gate "
                    + TraceContext.event_text(
                        "tool.profile.denied",
                        trace_id,
                        tool=name,
                        policy_scope="agent_profile",
                        policy_code="profile_tool_not_allowlisted",
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
            await self._audit_tool_event(
                trace_id=trace_id,
                event_type="tool.denied",
                tool_name=name,
                result=result,
            )
            return result

        if self._requires_gateway_envelope(name):
            if not self._request_context:
                result = ToolResult.failure(
                    ToolErrorKind.PERMISSION,
                    f"Tool '{name}' requires a signed gateway envelope",
                    code="gateway_envelope_required",
                    policy_scope="gateway",
                    skill_names=self._active_skill_names,
                    skill_permissions_mode=self._skill_permissions_mode,
                )
                await self._audit_tool_event(
                    trace_id=trace_id,
                    event_type="tool.denied",
                    tool_name=name,
                    result=result,
                )
                return result
            valid_envelope, envelope_reason = verify_gateway_envelope(self._request_context)
            if not valid_envelope:
                result = ToolResult.failure(
                    ToolErrorKind.PERMISSION,
                    f"Tool '{name}' requires a valid signed gateway envelope: {envelope_reason}",
                    code="gateway_envelope_invalid",
                    policy_scope="gateway",
                    skill_names=self._active_skill_names,
                    skill_permissions_mode=self._skill_permissions_mode,
                )
                await self._audit_tool_event(
                    trace_id=trace_id,
                    event_type="tool.denied",
                    tool_name=name,
                    result=result,
                )
                return result

        resource_decision = self._evaluate_resource_policy(name, params)
        if resource_decision is not None:
            await self._audit_tool_event(
                trace_id=trace_id,
                event_type="tool.denied",
                tool_name=name,
                result=resource_decision,
            )
            return resource_decision

        # Check Quota (per-tool and per-capability).
        if self._quota_engine:
            tenant_id = trace_id.split(":")[0] if trace_id and ":" in trace_id else "default"
            cap_limit: int | None = None
            cap_name = ""
            identity_ctx = self._identity_context()
            cap_request = capability_request_from_tool(name, params, identity_ctx)
            if cap_request is not None:
                cap_name = cap_request.capability
                policy = self._security_policy
                if policy is not None:
                    quotas_cfg = getattr(policy, "capability_quotas", None)
                    if quotas_cfg is not None:
                        cap_limit = quotas_cfg.limit_for_capability(cap_name)
            quota_ok = await self._quota_engine.check_quota(
                tenant_id, name, capability=cap_name, capability_limit=cap_limit,
            )
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

            exec_kwargs = dict(params)
            if self._tool_accepts_kwarg(tool, "trace_id"):
                exec_kwargs["trace_id"] = trace_id
            if self._tool_accepts_kwarg(tool, "security_context"):
                exec_kwargs["security_context"] = dict(self._request_context)
            raw_result = await tool.execute(**exec_kwargs)
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
            if self._audit_worker and trace_id:
                await self._audit_tool_event(
                    trace_id=trace_id,
                    event_type="tool.executed",
                    tool_name=name,
                    result=result,
                    elapsed_ms=elapsed_ms,
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
        if name in self._profile_denied_tools or "*" in self._profile_denied_tools:
            return False
        if self._profile_allowed_tools is not None:
            if "*" not in self._profile_allowed_tools and name not in self._profile_allowed_tools:
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

    def set_profile_tool_binding(
        self,
        *,
        allow: set[str] | list[str] | tuple[str, ...] | None = None,
        deny: set[str] | list[str] | tuple[str, ...] | None = None,
    ) -> None:
        """Set/replace the profile-level tool binding gate."""
        self._profile_allowed_tools = (
            {str(t).strip().lower() for t in allow if str(t).strip()} if allow is not None else None
        )
        self._profile_denied_tools = {str(t).strip().lower() for t in (deny or []) if str(t).strip()}

    def clear_profile_tool_binding(self) -> None:
        """Disable the profile-level tool binding gate."""
        self._profile_allowed_tools = None
        self._profile_denied_tools = set()

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

    def _evaluate_resource_policy(self, name: str, params: dict[str, Any]) -> ToolResult | None:
        policy = self._security_policy
        if policy is None:
            # GAP-9: if request context indicates production hardening but no
            # policy object is available, deny rather than silently allow.
            identity_context = self._identity_context()
            ps = dict(identity_context.get("policy_snapshot") or {})
            if ps.get("production_hardening"):
                return ToolResult.failure(
                    ToolErrorKind.PERMISSION,
                    "security policy not loaded under production hardening",
                    code="resource_scope_policy_missing",
                    policy_scope="resource",
                )
            return None
        identity_context = self._identity_context()
        request = capability_request_from_tool(name, params, identity_context)
        if request is None:
            return None
        grant_denial = self._evaluate_capability_grants(request.capability)
        if grant_denial is not None:
            return grant_denial
        evaluator = CapabilityPolicyEvaluator(policy, identity_context)
        decision = evaluator.evaluate(request)
        if decision.allowed:
            return None
        return evaluator.denial_result(
            decision,
            identity=self._audit_identity_summary(),
            dev_profile=bool(identity_context.get("dev_profile")),
            skill_names=self._active_skill_names,
            skill_permissions_mode=self._skill_permissions_mode,
        )

    def _identity_context(self) -> dict[str, Any]:
        return extract_security_context(self._request_context)

    def _evaluate_capability_grants(self, capability: str) -> ToolResult | None:
        identity_context = self._identity_context()
        subject_type = str(identity_context.get("subject_type") or "").strip().lower()
        if not subject_type or subject_type == "agent":
            return None
        grants = {
            str(item or "").strip().lower()
            for item in (identity_context.get("capability_grants") or [])
            if str(item or "").strip()
        }
        normalized = str(capability or "").strip().lower()
        if "*" in grants or normalized in grants:
            return None
        capability_family = normalized.split(".", 1)[0]
        if capability_family and f"{capability_family}.*" in grants:
            return None
        return ToolResult.failure(
            ToolErrorKind.PERMISSION,
            f"Subject '{subject_type}' is not granted capability '{normalized}'",
            code="capability_grant_denied",
            policy_scope="gateway_subject",
            capability=normalized,
            skill_names=self._active_skill_names,
            skill_permissions_mode=self._skill_permissions_mode,
        )

    def _requires_gateway_envelope(self, tool_name: str) -> bool:
        token = str(tool_name or "").strip().lower()
        return token in self._HIGH_RISK_TOOL_NAMES

    def _audit_identity_summary(self) -> dict[str, Any]:
        identity_context = self._identity_context()
        return {
            "channel": str(identity_context.get("channel") or ""),
            "sender_id": str(identity_context.get("sender_id") or ""),
            "chat_id": str(identity_context.get("chat_id") or ""),
            "tenant_id": str(identity_context.get("tenant_id") or ""),
            "workspace_id": str(identity_context.get("workspace_id") or ""),
            "agent_profile": str(identity_context.get("agent_profile") or ""),
            "role": str(identity_context.get("role") or ""),
            "channel_role": str(identity_context.get("channel_role") or ""),
            "subject_type": str(identity_context.get("subject_type") or ""),
            "trust_tier": str(identity_context.get("trust_tier") or ""),
        }

    @staticmethod
    def _tool_accepts_kwarg(tool: Tool, key: str) -> bool:
        try:
            params = inspect.signature(tool.execute).parameters.values()
        except (TypeError, ValueError):
            return False
        for param in params:
            if param.kind == inspect.Parameter.VAR_KEYWORD:
                return True
            if param.name == key:
                return True
        return False

    async def _audit_tool_event(
        self,
        *,
        trace_id: str | None,
        event_type: str,
        tool_name: str,
        result: ToolResult,
        elapsed_ms: int | None = None,
    ) -> None:
        if not self._audit_worker or not trace_id:
            return
        await self._audit_worker.audit_turn(
            trace_id,
            {
                "event_type": event_type,
                "tool": tool_name,
                "ok": result.ok,
                "elapsed_ms": elapsed_ms,
                "error_kind": result.error.kind.value if result.error else None,
                "error_code": result.error.code if result.error else None,
                "skill_names": self._active_skill_names or None,
                "identity": self._audit_identity_summary(),
                "policy_snapshot": dict(self._request_context.get("policy_snapshot") or {}),
                "dev_profile": bool(self._identity_context().get("dev_profile")),
                "capability": str(result.meta.get("capability") or ""),
                "resource_scope": dict(result.meta.get("resource_scope") or {}),
                "matched_scope": dict(result.meta.get("matched_scope") or {}),
                "sidecar_target": str(result.meta.get("sidecar_target") or ""),
                "approval": dict(result.meta.get("approval") or {}),
            },
        )

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
    _HIGH_RISK_TOOL_NAMES: frozenset[str] = frozenset(
        {
            "exec",
            "web_fetch",
            "web_search",
            "message",
            "social_platform_post",
            "social_platform_like",
            "browser_open",
            "browser_click",
            "browser_type",
            "browser_extract",
            "browser_screenshot",
            "browser_save_session",
            "browser_load_session",
            "sessions_spawn",
            "sessions_list",
            "sessions_kill",
            "sessions_read",
            "sessions_write",
            "sessions_signal",
            "sessions_resize",
        }
    )

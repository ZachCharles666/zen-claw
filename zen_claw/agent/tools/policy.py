"""Tool policy engine for layered allow/deny enforcement."""

from dataclasses import dataclass


@dataclass
class ToolPolicyScope:
    """Tool policy rules for a single scope."""

    allow: set[str] | None = None
    deny: set[str] | None = None


@dataclass
class ToolPolicyDecision:
    """Policy decision for a tool execution attempt."""

    allowed: bool
    reason: str
    code: str
    scope: str | None = None


class ToolPolicyEngine:
    """
    Layered tool policy engine.

    Rules:
    1) Deny always takes precedence over allow across all scopes.
    2) If any scope defines an allow list, the effective policy is allow-by-list.
    3) If no allow list exists, only tools in `default_deny_tools` are denied.
    """

    def __init__(self, default_deny_tools: set[str] | None = None):
        self.default_deny_tools = self._normalize_names(default_deny_tools or {"exec", "spawn"})
        self._scopes: dict[str, ToolPolicyScope] = {}

    def set_scope(
        self,
        scope: str,
        *,
        allow: set[str] | list[str] | tuple[str, ...] | None = None,
        deny: set[str] | list[str] | tuple[str, ...] | None = None,
    ) -> None:
        """Set or replace a scope policy."""
        allow_set = self._normalize_names(allow) if allow is not None else None
        deny_set = self._normalize_names(deny) if deny is not None else None
        self._scopes[scope] = ToolPolicyScope(allow=allow_set, deny=deny_set)

    def clear_scope(self, scope: str) -> None:
        """Remove a scope policy."""
        self._scopes.pop(scope, None)

    def evaluate(self, tool_name: str) -> ToolPolicyDecision:
        """Evaluate whether a tool is allowed under the merged layered policy."""
        name = (tool_name or "").strip().lower()
        deny_scope = self._find_deny_scope(name)
        if deny_scope:
            return ToolPolicyDecision(
                allowed=False,
                reason=f"Tool '{name}' denied by policy scope '{deny_scope}'",
                code="tool_policy_denied",
                scope=deny_scope,
            )

        allow_defined = False
        for scope in self._scopes.values():
            if scope.allow is not None:
                allow_defined = True
                if "*" in scope.allow or name in scope.allow:
                    return ToolPolicyDecision(
                        allowed=True,
                        reason="Allowed by explicit allow policy",
                        code="tool_policy_allowed",
                    )

        if allow_defined:
            return ToolPolicyDecision(
                allowed=False,
                reason=f"Tool '{name}' not in allow policy",
                code="tool_policy_not_allowlisted",
            )

        if name in self.default_deny_tools:
            return ToolPolicyDecision(
                allowed=False,
                reason=f"Tool '{name}' blocked by default deny policy",
                code="tool_policy_default_deny",
            )

        return ToolPolicyDecision(
            allowed=True,
            reason="Allowed by default policy",
            code="tool_policy_allowed",
        )

    def _find_deny_scope(self, tool_name: str) -> str | None:
        for scope_name, scope in self._scopes.items():
            deny = scope.deny
            if deny is not None and ("*" in deny or tool_name in deny):
                return scope_name
        return None

    def _normalize_names(self, names: set[str] | list[str] | tuple[str, ...]) -> set[str]:
        out: set[str] = set()
        for name in names:
            token = name.strip().lower()
            if token:
                out.add(token)
        return out

"""Declarative intent engine for simple, single-tool intents.

This module provides a data-driven alternative to the mixin-based handler pattern.
Each intent is defined as a single ``DeclarativeIntent`` instance that bundles:

- trigger patterns (regex, bilingual)
- parameter extraction rules
- tool call template
- contract
- failure message

The engine is invoked by ``route()`` *before* the native mixin-based registry,
so declarative intents take priority for simple queries while complex intents
(weather multi-source fallback, exchange reverse-solve, etc.) remain in the
existing handler code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from zen_claw.agent.intent_router_contracts import (
    IntentRouteResult,
    IntentToolContract,
    RecoveryBlocker,
    RecoveryOutcome,
    RecoveryPlan,
    RecoveryStrategy,
)
from zen_claw.agent.tools.registry import ToolRegistry


# ── Declarative intent definition ──────────────────────────────────────────────


@dataclass
class ParamRule:
    """Rule for extracting a single parameter from user input."""

    pattern: str | None = None  # Regex with a capture group; first group used
    default: Any = None  # Value when pattern doesn't match
    type: str = "str"  # "str" | "int" | "float" | "bool"

    def extract(self, content: str) -> Any:
        if self.pattern:
            m = re.search(self.pattern, content, re.IGNORECASE)
            if m:
                raw = m.group(1) if m.lastindex else m.group(0)
                return self._cast(raw)
        return self.default

    def _cast(self, raw: str) -> Any:
        if self.type == "int":
            try:
                return int(raw)
            except (ValueError, TypeError):
                return self.default
        if self.type == "float":
            try:
                return float(raw)
            except (ValueError, TypeError):
                return self.default
        if self.type == "bool":
            # Pattern matched → presence itself means True
            return True
        return raw


@dataclass
class DeclarativeIntent:
    """Complete definition of a simple, single-tool intent."""

    # Identity
    name: str
    description: str = ""

    # Parser — at least one pattern must match for the intent to trigger
    patterns: list[str] = field(default_factory=list)
    # Negative patterns — if any match, the intent is skipped (disambiguation)
    negative_patterns: list[str] = field(default_factory=list)

    # Parameter extraction from matched input
    params: dict[str, ParamRule] = field(default_factory=dict)
    # Static parameters always passed to the tool call
    static_params: dict[str, Any] = field(default_factory=dict)

    # Tool call
    tool_name: str = ""
    tool_action: str = ""

    # Contract
    contract: IntentToolContract | None = None

    # Response mode
    # "direct" = return tool result as-is
    # "hybrid" = return needs_constrained_replan (hand off to LLM + skill)
    mode: str = "direct"
    hybrid_skill: str | None = None  # skill to load when mode="hybrid"

    # Recovery
    failure_message: str = "Service temporarily unavailable."

    # Compiled regex cache (populated on first use)
    _compiled: list[re.Pattern[str]] | None = field(default=None, repr=False)
    _compiled_neg: list[re.Pattern[str]] | None = field(default=None, repr=False)

    def _ensure_compiled(self) -> None:
        if self._compiled is None:
            self._compiled = [re.compile(p, re.IGNORECASE) for p in self.patterns]
        if self._compiled_neg is None:
            self._compiled_neg = [re.compile(p, re.IGNORECASE) for p in self.negative_patterns]

    def match(self, content: str) -> bool:
        """Return True if content matches any positive pattern and no negative pattern."""
        self._ensure_compiled()
        assert self._compiled is not None
        assert self._compiled_neg is not None
        if any(p.search(content) for p in self._compiled_neg):
            return False
        return any(p.search(content) for p in self._compiled)

    def extract_params(self, content: str) -> dict[str, Any]:
        """Extract all parameters from content using param rules + static params."""
        result = dict(self.static_params)
        for key, rule in self.params.items():
            result[key] = rule.extract(content)
        return result


# ── Declarative routing engine ─────────────────────────────────────────────────


class DeclarativeIntentEngine:
    """Registry and executor for declarative intents."""

    def __init__(self) -> None:
        self._intents: list[DeclarativeIntent] = []

    def register(self, intent: DeclarativeIntent) -> None:
        """Register a declarative intent definition."""
        self._intents.append(intent)

    def register_many(self, intents: list[DeclarativeIntent]) -> None:
        """Register multiple declarative intents."""
        self._intents.extend(intents)

    @property
    def intent_names(self) -> list[str]:
        return [i.name for i in self._intents]

    async def route(
        self,
        content: str,
        *,
        tools: ToolRegistry,
        trace_id: str,
    ) -> IntentRouteResult | None:
        """Try to match and execute a declarative intent.

        Returns ``None`` if no declarative intent matches (caller should
        proceed to native mixin-based routing).
        """
        for intent in self._intents:
            if not intent.match(content):
                continue

            if intent.mode == "hybrid":
                return self._build_hybrid_result(intent, content)

            # Direct mode: execute tool call
            params = intent.extract_params(content)
            if intent.tool_action:
                params.setdefault("action", intent.tool_action)

            try:
                result = await tools.execute(intent.tool_name, params, trace_id=trace_id)
            except Exception as exc:
                return self._build_failure(intent, str(exc), trace_id)

            if result.ok:
                return IntentRouteResult(
                    handled=True,
                    intent_name=intent.name,
                    content=result.content,
                    contract=intent.contract,
                    route_status="direct_success",
                    diagnostic=f"declarative:{intent.name}:ok",
                )
            else:
                error_msg = ""
                if result.error:
                    error_msg = result.error.message or ""
                return self._build_failure(intent, error_msg, trace_id)

        return None  # No match — fall through to native handlers

    @staticmethod
    def _build_hybrid_result(intent: DeclarativeIntent, content: str) -> IntentRouteResult:
        """Build a needs_constrained_replan result for hybrid intents."""
        return IntentRouteResult(
            handled=True,
            intent_name=intent.name,
            content=content,
            contract=intent.contract,
            route_status="needs_constrained_replan",
            diagnostic=f"declarative:{intent.name}:hybrid",
            skip_planning=False,
        )

    @staticmethod
    def _build_failure(intent: DeclarativeIntent, error: str, trace_id: str) -> IntentRouteResult:
        """Build a direct_failed result with recovery guidance."""
        plan = RecoveryPlan(
            blocker=RecoveryBlocker(
                kind="upstream_unavailable",
                description=error or intent.failure_message,
                missing_requirement=f"tool:{intent.tool_name}",
            ),
            strategies=[
                RecoveryStrategy(kind="guidance_only", detail=intent.failure_message)
            ],
            checked_scope=[f"tool:{intent.tool_name}"],
            next_steps=[intent.failure_message],
            fallback_options=[],
        )
        outcome = RecoveryOutcome(
            mode="failed",
            content=intent.failure_message + (f"\n({error})" if error else ""),
            plan=plan,
        )
        return IntentRouteResult(
            handled=True,
            intent_name=intent.name,
            content=outcome.content,
            contract=intent.contract,
            route_status="direct_failed",
            diagnostic=f"declarative:{intent.name}:failed:{trace_id}",
            recovery_outcome=outcome,
        )

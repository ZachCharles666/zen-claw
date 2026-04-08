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
    ControlSignals,
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
        value, _ranges = self.extract_with_trace(content)
        return value

    def extract_with_trace(self, content: str) -> tuple[Any, list[tuple[int, int]]]:
        if self.pattern:
            m = re.search(self.pattern, content, re.IGNORECASE)
            if m:
                raw = m.group(1) if m.lastindex else m.group(0)
                span = m.span(1) if m.lastindex else m.span(0)
                return self._cast(raw), [span]
        return self.default, []

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
        params, _trace = self.extract_params_with_trace(content)
        return params

    def extract_params_with_trace(self, content: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """Extract all parameters from content using param rules + static params."""
        result = dict(self.static_params)
        consumed_ranges: list[tuple[int, int]] = []
        matched_rule_count = 0
        for key, rule in self.params.items():
            value, ranges = rule.extract_with_trace(content)
            result[key] = value
            consumed_ranges.extend(ranges)
            if ranges:
                matched_rule_count += 1
        return result, {
            "consumed_ranges": consumed_ranges,
            "consumed_char_count": _count_covered_characters(consumed_ranges),
            "param_rule_count": len(self.params),
            "matched_rule_count": matched_rule_count,
        }

    def build_route_candidate(self) -> dict[str, Any]:
        """Build Gate-migration candidate metadata for this declarative match."""
        raw_confidence = 0.75 if self.mode == "hybrid" else 0.9
        return {
            "match": self.name,
            "source": "declarative",
            "raw_confidence": raw_confidence,
        }


# ── Declarative routing engine ─────────────────────────────────────────────────


class DeclarativeIntentEngine:
    """Registry and executor for declarative intents."""

    _SAFETY_VALVE_THRESHOLD = 0.8

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

    def get_intent(self, name: str) -> DeclarativeIntent | None:
        target = str(name or "").strip()
        for intent in self._intents:
            if intent.name == target:
                return intent
        return None

    async def route(
        self,
        content: str,
        *,
        tools: ToolRegistry,
        trace_id: str,
        control_signals: ControlSignals | dict[str, Any] | None = None,
    ) -> IntentRouteResult | None:
        """Try to match and execute a declarative intent.

        Returns ``None`` if no declarative intent matches (caller should
        proceed to native mixin-based routing).
        """
        matched_intents = [intent for intent in self._intents if intent.match(content)]
        if not matched_intents:
            return None

        matched_names = [intent.name for intent in matched_intents]
        intent = matched_intents[0]
        route_candidate = intent.build_route_candidate()
        route_candidate["matched_candidates"] = matched_names

        return await self.route_via_candidate(
            intent=intent,
            content=content,
            tools=tools,
            trace_id=trace_id,
            control_signals=control_signals,
            route_candidate=route_candidate,
        )

    async def route_via_candidate(
        self,
        *,
        intent: DeclarativeIntent,
        content: str,
        tools: ToolRegistry,
        trace_id: str,
        control_signals: ControlSignals | dict[str, Any] | None = None,
        route_candidate: dict[str, Any] | None = None,
    ) -> IntentRouteResult:
        route_candidate = dict(route_candidate or intent.build_route_candidate())
        route_candidate.setdefault("match", intent.name)
        route_candidate.setdefault("matched_candidates", [intent.name])

        if intent.mode == "hybrid":
            return self._build_hybrid_result(intent, content, route_candidate)

        params, extraction_trace = intent.extract_params_with_trace(content)
        safety_trace = self._evaluate_safety_valve(
            intent=intent,
            content=content,
            route_candidate=route_candidate,
            control_signals=control_signals,
            extraction_trace=extraction_trace,
        )
        delegate_reason = str(safety_trace.get("delegate_reason") or "").strip() or None
        if delegate_reason is not None:
            return self._build_delegate_result(
                intent=intent,
                content=content,
                route_candidate=route_candidate,
                delegate_reason=delegate_reason,
                safety_valve_trace=safety_trace,
            )

        # Direct mode: execute tool call
        if intent.tool_action:
            params.setdefault("action", intent.tool_action)

        try:
            result = await tools.execute(intent.tool_name, params, trace_id=trace_id)
        except Exception as exc:
            return self._build_failure(
                intent,
                str(exc),
                trace_id,
                route_candidate=route_candidate,
                safety_valve_outcome="execute",
                safety_valve_trace=safety_trace,
            )

        if result.ok:
            return IntentRouteResult(
                handled=True,
                intent_name=intent.name,
                content=result.content,
                contract=intent.contract,
                route_status="direct_success",
                diagnostic=f"declarative:{intent.name}:ok",
                route_candidate=route_candidate,
                safety_valve_outcome="execute",
                safety_valve_trace=safety_trace,
            )
        error_msg = ""
        if result.error:
            error_msg = result.error.message or ""
        return self._build_failure(
            intent,
            error_msg,
            trace_id,
            route_candidate=route_candidate,
            safety_valve_outcome="execute",
            safety_valve_trace=safety_trace,
        )

    @staticmethod
    def _build_hybrid_result(
        intent: DeclarativeIntent, content: str, route_candidate: dict[str, Any]
    ) -> IntentRouteResult:
        """Build a needs_constrained_replan result for hybrid intents."""
        return IntentRouteResult(
            handled=True,
            intent_name=intent.name,
            content=content,
            contract=intent.contract,
            route_status="needs_constrained_replan",
            diagnostic=f"declarative:{intent.name}:hybrid",
            skip_planning=False,
            route_candidate=route_candidate,
        )

    @staticmethod
    def _build_failure(
        intent: DeclarativeIntent,
        error: str,
        trace_id: str,
        *,
        route_candidate: dict[str, Any],
        safety_valve_outcome: str | None = None,
        safety_valve_trace: dict[str, Any] | None = None,
    ) -> IntentRouteResult:
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
            route_candidate=route_candidate,
            safety_valve_outcome=safety_valve_outcome,
            safety_valve_trace=safety_valve_trace,
        )

    def _evaluate_safety_valve(
        self,
        *,
        intent: DeclarativeIntent,
        content: str,
        route_candidate: dict[str, Any],
        control_signals: ControlSignals | dict[str, Any] | None,
        extraction_trace: dict[str, Any],
    ) -> dict[str, Any]:
        history_confidence = 1.0
        if isinstance(control_signals, ControlSignals):
            history_map = control_signals.history_confidence
        elif isinstance(control_signals, dict):
            history_map = control_signals.get("history_confidence") or {}
        else:
            history_map = {}
        raw_value = history_map.get(intent.name)
        try:
            if raw_value is not None:
                history_confidence = float(raw_value)
        except (TypeError, ValueError):
            history_confidence = 1.0
        raw_confidence = float(route_candidate.get("raw_confidence") or 0.0)
        consumed_char_count = int(extraction_trace.get("consumed_char_count") or 0)
        param_rule_count = int(extraction_trace.get("param_rule_count") or 0)
        matched_rule_count = int(extraction_trace.get("matched_rule_count") or 0)
        normalized_length = len(_normalized_for_residual(content))
        residual_ratio = 0.0
        if (
            normalized_length >= 16
            and param_rule_count > 0
            and matched_rule_count > 0
        ):
            residual_ratio = max(0.0, min(1.0, 1 - (consumed_char_count / normalized_length)))
        matched_candidates = list(route_candidate.get("matched_candidates") or [])
        multiple_hits = len(matched_candidates) > 1
        has_pronoun_signal = _contains_pronoun_reference(content)
        # Dynamic length-outlier threshold: adapts to typical pattern length of
        # the matched intent; falls back to 28 chars if not available.
        typical_input_length = int(extraction_trace.get("typical_input_length") or 0)
        length_outlier_threshold = max(28, 2 * typical_input_length) if typical_input_length else 28
        long_input = normalized_length >= length_outlier_threshold

        weighted_confidence = raw_confidence * history_confidence
        penalties = 0.0
        delegate_reason = ""
        if multiple_hits:
            # Reduced from 0.45: high-confidence single-domain requests were
            # being over-penalised when two overlapping patterns fired.
            penalties += 0.30
            delegate_reason = "safety_valve_multiple_candidates"
        if not delegate_reason and has_pronoun_signal:
            penalties += 0.25
            delegate_reason = "safety_valve_context_pronoun"
        if residual_ratio >= 0.45:
            penalties += min(0.35, residual_ratio * 0.5)
            if not delegate_reason:
                delegate_reason = "safety_valve_high_residual_ratio"
        if long_input:
            penalties += 0.15
            if not delegate_reason:
                delegate_reason = "safety_valve_length_outlier"
        # Penalty stacking cap: prevents multiple weak signals from
        # over-penalising an otherwise clear-cut request.
        penalties = min(0.65, penalties)
        final_confidence = max(0.0, weighted_confidence - penalties)
        if not delegate_reason and final_confidence < self._SAFETY_VALVE_THRESHOLD:
            delegate_reason = "safety_valve_low_confidence"
        return {
            "raw_confidence": raw_confidence,
            "history_confidence": history_confidence,
            "weighted_confidence": round(weighted_confidence, 4),
            "final_confidence": round(final_confidence, 4),
            "residual_ratio": round(residual_ratio, 4),
            "consumed_char_count": consumed_char_count,
            "input_char_count": normalized_length,
            "matched_candidates_count": len(matched_candidates),
            "matched_candidates": matched_candidates,
            "has_context_pronoun": has_pronoun_signal,
            "length_outlier": long_input,
            "length_outlier_threshold": length_outlier_threshold,
            "delegate_reason": delegate_reason or None,
        }

    @staticmethod
    def _build_delegate_result(
        *,
        intent: DeclarativeIntent,
        content: str,
        route_candidate: dict[str, Any],
        delegate_reason: str,
        safety_valve_trace: dict[str, Any] | None,
    ) -> IntentRouteResult:
        return IntentRouteResult(
            handled=True,
            intent_name=intent.name,
            content=content,
            contract=intent.contract,
            route_status="needs_constrained_replan",
            diagnostic=f"declarative:{intent.name}:delegate:{delegate_reason}",
            route_candidate=route_candidate,
            delegate_reason=delegate_reason,
            safety_valve_outcome="delegate",
            safety_valve_trace=safety_valve_trace,
        )


def _count_covered_characters(ranges: list[tuple[int, int]]) -> int:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if end <= start:
            continue
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        prev_start, prev_end = merged[-1]
        merged[-1] = (prev_start, max(prev_end, end))
    return sum(end - start for start, end in merged)


def _normalized_for_residual(content: str) -> str:
    return "".join(ch for ch in content if not ch.isspace())


def _contains_pronoun_reference(content: str) -> bool:
    lowered = str(content or "").strip().lower()
    if not lowered:
        return False
    signals = (
        "那个",
        "这个",
        "刚才",
        "之前那个",
        "按之前",
        "that one",
        "the previous",
        "same as before",
        "use that",
    )
    return any(signal in lowered for signal in signals)

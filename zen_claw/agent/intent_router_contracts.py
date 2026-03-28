"""Contracts and result types for the intent router."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class IntentToolContract:
    """Runtime-enforced tool contract for a recognized intent."""

    intent_name: str
    preferred_tools: list[str]
    allowed_tools: set[str]
    denied_tools: set[str]
    version: int = 1
    intent_mode: Literal["router_first", "skill_first", "hybrid"] = "skill_first"
    allow_constrained_replan: bool = True
    allow_high_risk_escalation: bool = False
    response_mode: Literal["direct", "llm_assisted"] = "direct"
    failure_mode: Literal["runtime_direct", "runtime_fact_llm_format"] = "runtime_direct"
    fact_payload_schema: dict[str, Any] | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "IntentToolContract | None":
        """Build a runtime contract from structured metadata."""
        if not isinstance(payload, dict):
            return None
        intent_name = str(payload.get("intent") or payload.get("intent_name") or "").strip()
        version = payload.get("version", 1)
        if not isinstance(version, int) or version < 1:
            return None
        intent_mode = str(payload.get("intent_mode") or "skill_first").strip().lower()
        if intent_mode not in {"router_first", "skill_first", "hybrid"}:
            return None
        preferred_tools = cls._normalize_tool_list(payload.get("preferred_tools"))
        allowed_tools = set(cls._normalize_tool_list(payload.get("allowed_tools")))
        denied_tools = set(cls._normalize_tool_list(payload.get("denied_tools")))
        response_mode = str(payload.get("response_mode") or "direct").strip().lower()
        if response_mode not in {"direct", "llm_assisted"}:
            return None
        failure_mode = str(payload.get("failure_mode") or "runtime_direct").strip().lower()
        if failure_mode not in {"runtime_direct", "runtime_fact_llm_format"}:
            return None
        fact_payload_schema = payload.get("fact_payload_schema")
        if fact_payload_schema is not None and not isinstance(fact_payload_schema, dict):
            return None
        if not intent_name or not allowed_tools:
            return None
        if preferred_tools:
            preferred_tools = [tool for tool in preferred_tools if tool in allowed_tools]
        if not preferred_tools:
            preferred_tools = sorted(allowed_tools)
        return cls(
            intent_name=intent_name,
            version=version,
            intent_mode=intent_mode,
            preferred_tools=preferred_tools,
            allowed_tools=allowed_tools,
            denied_tools=denied_tools,
            allow_constrained_replan=bool(payload.get("allow_constrained_replan", True)),
            allow_high_risk_escalation=bool(payload.get("allow_high_risk_escalation", False)),
            response_mode=response_mode,
            failure_mode=failure_mode,
            fact_payload_schema=fact_payload_schema if isinstance(fact_payload_schema, dict) else None,
        )

    @staticmethod
    def _normalize_tool_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        out: list[str] = []
        for item in value:
            text = str(item or "").strip().lower()
            if text and text not in out:
                out.append(text)
        return out


@dataclass
class IntentRouteResult:
    """Outcome of a pre-LLM routing attempt."""

    handled: bool
    intent_name: str | None = None
    content: str | None = None
    contract: IntentToolContract | None = None
    route_status: Literal[
        "miss",
        "direct_success",
        "direct_failed",
        "needs_constrained_replan",
        "needs_explicit_approval",
    ] = "miss"
    diagnostic: str | None = None
    skip_planning: bool = False
    recovery_outcome: "RecoveryOutcome | None" = None
    route_candidate: dict[str, Any] | None = None
    delegate_reason: str | None = None
    safety_valve_outcome: str | None = None
    arbitration_result: str | None = None
    safety_valve_trace: dict[str, Any] | None = None


@dataclass(frozen=True)
class ControlSignals:
    """Explicit control-plane contract for Gate 1 / Safety Valve."""

    history_confidence: dict[str, float]
    emotion_signal: dict[str, Any] | None = None
    identity_signal: dict[str, Any] | None = None
    extra: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "history_confidence": dict(self.history_confidence),
            "emotion_signal": dict(self.emotion_signal or {}),
            "identity_signal": dict(self.identity_signal or {}),
            "extra": dict(self.extra or {}),
        }


@dataclass(frozen=True)
class IntentArbitrationResult:
    """Structured Gate 2 classification result."""

    kind: Literal[
        "confirm_candidate",
        "select_skill",
        "request_clarification",
        "unclassified",
    ]
    candidate_name: str | None = None
    skill_name: str | None = None
    clarification_question: str | None = None
    diagnostic: str | None = None


@dataclass
class SourceFallbackResult:
    """Result of trying an ordered list of low-risk data sources."""

    value: Any | None
    winner: str | None = None
    attempts: list[str] | None = None


@dataclass(frozen=True)
class RetryPolicy:
    """Minimal retry policy for low-risk direct intent fetches."""

    max_attempts: int = 2


@dataclass(frozen=True)
class ExchangeRateResolution:
    """Exchange-rate value with the path used to resolve it."""

    rate: float
    recovery_kind: Literal["direct", "fallback_source", "reverse_solve"] = "direct"


@dataclass(frozen=True)
class RecoveryGuidance:
    """Structured guidance for deterministic-but-helpful direct failures."""

    blocker: str
    missing_requirement: str
    checked_scope: list[str]
    next_steps: list[str]
    fallback_options: list[str]

    @classmethod
    def from_plan(cls, plan: "RecoveryPlan") -> "RecoveryGuidance":
        return cls(
            blocker=plan.blocker.description,
            missing_requirement=plan.blocker.missing_requirement,
            checked_scope=list(plan.checked_scope),
            next_steps=list(plan.next_steps),
            fallback_options=list(plan.fallback_options),
        )


@dataclass(frozen=True)
class RecoveryBlocker:
    """Normalized blocker classification for direct-intent recovery."""

    kind: Literal[
        "input_ambiguous",
        "source_scope_insufficient",
        "upstream_unavailable",
        "environment_missing",
        "locally_correctable",
        "permission_required",
    ]
    description: str
    missing_requirement: str


@dataclass(frozen=True)
class RecoveryStrategy:
    """A concrete strategy the router can use or suggest."""

    kind: Literal[
        "fallback_source",
        "same_site_search",
        "reverse_solve",
        "semantic_reroute",
        "local_correction",
        "guidance_only",
    ]
    detail: str


@dataclass(frozen=True)
class RecoveryPlan:
    """Minimal structured recovery plan for Phase 1 framework extraction."""

    blocker: RecoveryBlocker
    strategies: list[RecoveryStrategy]
    checked_scope: list[str]
    next_steps: list[str]
    fallback_options: list[str]


@dataclass(frozen=True)
class RecoveryOutcome:
    """Normalized recovery result shape for future framework expansion."""

    mode: Literal["resolved", "guided", "failed"]
    content: str
    plan: RecoveryPlan | None = None

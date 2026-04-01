"""Lightweight execution-layer contracts that sit after intent routing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from zen_claw.agent.intent_router_contracts import IntentRouteResult


@dataclass(frozen=True)
class RouteCandidate:
    """Structured Gate 1 candidate view derived from the compatibility payload."""

    match: str
    source: str
    raw_confidence: float | None = None
    crystallized_route: str | None = None
    evidence: dict[str, Any] | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> "RouteCandidate | None":
        if not isinstance(payload, dict):
            return None
        match = str(payload.get("match") or "").strip()
        source = str(payload.get("source") or "").strip()
        raw_value = payload.get("raw_confidence")
        raw_confidence = None
        if isinstance(raw_value, (int, float)):
            raw_confidence = float(raw_value)
        evidence = {
            key: value
            for key, value in payload.items()
            if key not in {"match", "source", "raw_confidence", "crystallized_route"}
        }
        return cls(
            match=match,
            source=source,
            raw_confidence=raw_confidence,
            crystallized_route=str(payload.get("crystallized_route") or "").strip() or None,
            evidence=evidence or None,
        )

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "match": self.match,
            "source": self.source,
        }
        if self.raw_confidence is not None:
            payload["raw_confidence"] = self.raw_confidence
        if self.crystallized_route:
            payload["crystallized_route"] = self.crystallized_route
        if self.evidence:
            payload["evidence"] = dict(self.evidence)
        return payload


@dataclass(frozen=True)
class RoutingDecision:
    """Decision-layer output that answers whether to execute or delegate."""

    action: Literal["execute", "delegate"]
    route_status: str
    routing_stage: str
    entered_gate3: bool
    intent_name: str | None = None
    delegate_reason: str | None = None
    safety_valve_outcome: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "route_status": self.route_status,
            "routing_stage": self.routing_stage,
            "entered_gate3": self.entered_gate3,
            "intent_name": self.intent_name or "",
            "delegate_reason": self.delegate_reason or "",
            "safety_valve_outcome": self.safety_valve_outcome or "",
        }


@dataclass(frozen=True)
class ArbitrationDecision:
    """Structured Gate 2 outcome carried into execution planning."""

    kind: Literal["confirm_candidate", "select_skill", "request_clarification", "unclassified", ""]
    candidate_name: str | None = None
    skill_name: str | None = None
    clarification_question: str | None = None
    diagnostic: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "candidate_name": self.candidate_name or "",
            "skill_name": self.skill_name or "",
            "clarification_question": self.clarification_question or "",
            "diagnostic": self.diagnostic or "",
        }


@dataclass(frozen=True)
class ExecutionIntent:
    """Execution-layer handoff produced after routing is finished."""

    path_type: Literal["direct", "constrained_replan", "skill_path", "gate3_plan", "clarification", "approval_wait", "agent_loop"]
    execution_mode: Literal["runtime_direct", "llm_constrained", "skill_guided", "llm_free_plan", "clarification", "approval_wait", "agent_loop"]
    decision_source: Literal["intent_router", "gate2", "gate3", "approval_gate"]
    operation_kind: Literal["read_only", "state_mutating", "external_side_effect", "requires_approval"]
    concurrency_class: Literal["parallel_safe", "serial_required"]
    contract_intent: str | None = None
    allowed_tools: list[str] | None = None
    denied_tools: list[str] | None = None
    approval_required: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "path_type": self.path_type,
            "execution_mode": self.execution_mode,
            "decision_source": self.decision_source,
            "operation_kind": self.operation_kind,
            "concurrency_class": self.concurrency_class,
            "contract_intent": self.contract_intent or "",
            "allowed_tools": list(self.allowed_tools or []),
            "denied_tools": list(self.denied_tools or []),
            "approval_required": self.approval_required,
        }


@dataclass(frozen=True)
class ExecutionResult:
    """Execution-layer result kept separate from routing decisions."""

    status: Literal["succeeded", "failed", "clarified", "approval_required", "delegated"]
    final_content: str | None = None
    failure_classification: str | None = None
    trace_summary: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "final_content": self.final_content or "",
            "failure_classification": self.failure_classification or "",
            "trace_summary": dict(self.trace_summary or {}),
        }


def resolve_routing_stage(route_result: IntentRouteResult) -> tuple[str, bool]:
    arbitration = str(route_result.arbitration_result or "").strip().lower()
    diagnostic = str(route_result.diagnostic or "")
    if arbitration == "request_clarification":
        return "gate2_clarify", False
    if arbitration == "unclassified" and "gate3_entry" in diagnostic:
        return "gate3_plan", True
    if route_result.safety_valve_outcome == "delegate" or arbitration in {
        "confirm_candidate",
        "select_skill",
        "unclassified",
    }:
        return "gate2_delegate", False
    if route_result.route_status in {"direct_success", "direct_failed"}:
        return "gate1_execute", False
    return "", False


def build_routing_decision(route_result: IntentRouteResult) -> RoutingDecision:
    routing_stage, entered_gate3 = resolve_routing_stage(route_result)
    action: Literal["execute", "delegate"] = "execute"
    if route_result.safety_valve_outcome == "delegate" or routing_stage in {
        "gate2_delegate",
        "gate2_clarify",
        "gate3_plan",
    }:
        action = "delegate"
    return RoutingDecision(
        action=action,
        route_status=route_result.route_status,
        routing_stage=routing_stage,
        entered_gate3=entered_gate3,
        intent_name=route_result.intent_name,
        delegate_reason=route_result.delegate_reason,
        safety_valve_outcome=route_result.safety_valve_outcome,
    )


def build_arbitration_decision(
    route_result: IntentRouteResult,
    *,
    clarification_question: str | None = None,
    selected_skill: str | None = None,
) -> ArbitrationDecision:
    kind = str(route_result.arbitration_result or "").strip().lower()
    if kind not in {"confirm_candidate", "select_skill", "request_clarification", "unclassified"}:
        kind = ""
    candidate_name = None
    route_candidate = RouteCandidate.from_payload(route_result.route_candidate)
    if kind == "confirm_candidate" and route_candidate is not None:
        candidate_name = route_candidate.match or None
    return ArbitrationDecision(
        kind=kind,  # type: ignore[arg-type]
        candidate_name=candidate_name,
        skill_name=selected_skill if kind == "select_skill" else None,
        clarification_question=clarification_question if kind == "request_clarification" else None,
        diagnostic=str(route_result.diagnostic or "").strip() or None,
    )


def build_execution_intent(
    route_result: IntentRouteResult,
    *,
    clarification_question: str | None = None,
    selected_skill: str | None = None,
) -> ExecutionIntent:
    contract = route_result.contract
    allowed_tools = sorted(contract.allowed_tools) if contract is not None else []
    denied_tools = sorted(contract.denied_tools) if contract is not None else []
    arbitration = str(route_result.arbitration_result or "").strip().lower()
    if route_result.route_status == "needs_explicit_approval":
        return ExecutionIntent(
            path_type="approval_wait",
            execution_mode="approval_wait",
            decision_source="approval_gate",
            operation_kind="requires_approval",
            concurrency_class="serial_required",
            contract_intent=contract.intent_name if contract is not None else route_result.intent_name,
            allowed_tools=allowed_tools,
            denied_tools=denied_tools,
            approval_required=True,
        )
    if clarification_question is not None or arbitration == "request_clarification":
        return ExecutionIntent(
            path_type="clarification",
            execution_mode="clarification",
            decision_source="gate2",
            operation_kind="read_only",
            concurrency_class="parallel_safe",
            contract_intent=contract.intent_name if contract is not None else route_result.intent_name,
            allowed_tools=allowed_tools,
            denied_tools=denied_tools,
        )
    if arbitration == "select_skill" and selected_skill:
        return ExecutionIntent(
            path_type="skill_path",
            execution_mode="skill_guided",
            decision_source="gate2",
            operation_kind="external_side_effect",
            concurrency_class="serial_required",
            contract_intent=selected_skill,
            allowed_tools=allowed_tools,
            denied_tools=denied_tools,
        )
    if arbitration == "unclassified":
        return ExecutionIntent(
            path_type="gate3_plan",
            execution_mode="llm_free_plan",
            decision_source="gate3",
            operation_kind="external_side_effect",
            concurrency_class="serial_required",
            contract_intent=contract.intent_name if contract is not None else route_result.intent_name,
            allowed_tools=allowed_tools,
            denied_tools=denied_tools,
        )
    if route_result.route_status in {"direct_success", "direct_failed"}:
        return ExecutionIntent(
            path_type="direct",
            execution_mode="runtime_direct",
            decision_source="intent_router",
            operation_kind="read_only",
            concurrency_class="parallel_safe",
            contract_intent=contract.intent_name if contract is not None else route_result.intent_name,
            allowed_tools=allowed_tools,
            denied_tools=denied_tools,
        )
    if route_result.route_status == "needs_constrained_replan":
        return ExecutionIntent(
            path_type="constrained_replan",
            execution_mode="llm_constrained",
            decision_source="intent_router" if arbitration != "confirm_candidate" else "gate2",
            operation_kind="external_side_effect",
            concurrency_class="serial_required",
            contract_intent=contract.intent_name if contract is not None else route_result.intent_name,
            allowed_tools=allowed_tools,
            denied_tools=denied_tools,
        )
    return ExecutionIntent(
        path_type="agent_loop",
        execution_mode="agent_loop",
        decision_source="gate3",
        operation_kind="external_side_effect",
        concurrency_class="serial_required",
        contract_intent=contract.intent_name if contract is not None else route_result.intent_name,
        allowed_tools=allowed_tools,
        denied_tools=denied_tools,
    )


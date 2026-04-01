from __future__ import annotations

import json
from pathlib import Path

from zen_claw.agent.intent_router_contracts import IntentRouteResult, IntentToolContract
from zen_claw.agent.loop import AgentLoop
from zen_claw.agent.orchestration import (
    RouteCandidate,
    build_arbitration_decision,
    build_execution_intent,
    build_routing_decision,
)
from zen_claw.bus.events import InboundMessage
from zen_claw.bus.queue import MessageBus
from zen_claw.providers.base import LLMProvider


def _delegate_route_result() -> IntentRouteResult:
    return IntentRouteResult(
        handled=True,
        intent_name="email_check",
        contract=IntentToolContract(
            intent_name="email_check",
            preferred_tools=["email"],
            allowed_tools={"email"},
            denied_tools={"exec"},
        ),
        route_status="needs_constrained_replan",
        diagnostic="delegate_for_gate2",
        route_candidate={
            "match": "email_check",
            "source": "declarative",
            "raw_confidence": 0.9,
        },
        delegate_reason="safety_valve_low_confidence",
        safety_valve_outcome="delegate",
        arbitration_result="select_skill",
    )


def test_build_routing_and_execution_contracts_separately() -> None:
    route_result = _delegate_route_result()

    candidate = RouteCandidate.from_payload(route_result.route_candidate)
    routing = build_routing_decision(route_result)
    arbitration = build_arbitration_decision(route_result, selected_skill="compliance_check")
    execution = build_execution_intent(route_result, selected_skill="compliance_check")

    assert candidate is not None
    assert candidate.match == "email_check"
    assert routing.action == "delegate"
    assert routing.routing_stage == "gate2_delegate"
    assert arbitration.kind == "select_skill"
    assert arbitration.skill_name == "compliance_check"
    assert execution.path_type == "skill_path"
    assert execution.execution_mode == "skill_guided"
    assert execution.decision_source == "gate2"
    assert execution.contract_intent == "compliance_check"
    assert execution.allowed_tools == ["email"]


def test_build_execution_intent_for_gate3_and_approval() -> None:
    unclassified = _delegate_route_result()
    unclassified.arbitration_result = "unclassified"
    unclassified.contract = IntentToolContract(
        intent_name="default_contract",
        preferred_tools=["read_file"],
        allowed_tools={"read_file"},
        denied_tools={"exec"},
    )
    gate3_execution = build_execution_intent(unclassified)
    assert gate3_execution.path_type == "gate3_plan"
    assert gate3_execution.execution_mode == "llm_free_plan"

    approval = _delegate_route_result()
    approval.route_status = "needs_explicit_approval"
    approval_execution = build_execution_intent(approval)
    assert approval_execution.path_type == "approval_wait"
    assert approval_execution.approval_required is True


class _SilentProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key=None, api_base=None)

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        raise AssertionError("chat should not be called")

    def get_default_model(self) -> str:
        return "fake-model"


def test_append_execution_event_persists_trace_summary(monkeypatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)

    loop = AgentLoop(
        bus=MessageBus(),
        provider=_SilentProvider(),
        workspace=tmp_path,
        model="fake-model",
    )
    event_msg = InboundMessage(
        channel="cli",
        chat_id="direct",
        sender_id="user",
        content="status",
    )
    route_result = _delegate_route_result()
    execution_intent = build_execution_intent(route_result, selected_skill="compliance_check")

    loop._append_execution_event(
        trace_id="trace-exec-summary",
        msg=event_msg,
        route_result=route_result,
        execution_intent=execution_intent,
        execution_result_status="failed",
        final_content="tool execution failed",
        selected_model="primary-model",
        model_reason="intent_override",
        failure_classification="tool_runtime",
        trace_summary={
            "iterations": 2,
            "tool_calls": 3,
            "tool_failures": 1,
            "approval_waited": False,
        },
    )

    log_path = data_dir / "dashboard" / "agent_execution.log.jsonl"
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    summary = rows[-1]["execution_result"]["trace_summary"]
    assert summary["selected_model"] == "primary-model"
    assert summary["model_reason"] == "intent_override"
    assert summary["iterations"] == 2
    assert summary["tool_calls"] == 3
    assert summary["tool_failures"] == 1


def test_append_execution_event_normalizes_failure_taxonomy(monkeypatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)

    loop = AgentLoop(
        bus=MessageBus(),
        provider=_SilentProvider(),
        workspace=tmp_path,
        model="fake-model",
    )
    event_msg = InboundMessage(
        channel="cli",
        chat_id="direct",
        sender_id="user",
        content="status",
    )
    route_result = _delegate_route_result()
    execution_intent = build_execution_intent(route_result, selected_skill="compliance_check")

    loop._append_execution_event(
        trace_id="trace-exec-taxonomy",
        msg=event_msg,
        route_result=route_result,
        execution_intent=execution_intent,
        execution_result_status="failed",
        final_content="timed out",
        selected_model="primary-model",
        model_reason="intent_override",
        failure_classification="tool_timeout",
        trace_summary={
            "last_error_kind": "retryable",
            "last_error_code": "request_timeout",
        },
    )

    log_path = data_dir / "dashboard" / "agent_execution.log.jsonl"
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    result = rows[-1]["execution_result"]
    assert result["failure_classification"] == "tool_timeout"


def test_append_execution_event_preserves_failure_detail_when_normalized(
    monkeypatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)

    loop = AgentLoop(
        bus=MessageBus(),
        provider=_SilentProvider(),
        workspace=tmp_path,
        model="fake-model",
    )
    event_msg = InboundMessage(
        channel="cli",
        chat_id="direct",
        sender_id="user",
        content="status",
    )
    route_result = _delegate_route_result()
    execution_intent = build_execution_intent(route_result, selected_skill="compliance_check")

    loop._append_execution_event(
        trace_id="trace-exec-detail",
        msg=event_msg,
        route_result=route_result,
        execution_intent=execution_intent,
        execution_result_status="failed",
        final_content="missing output",
        selected_model="primary-model",
        model_reason="intent_override",
        failure_classification="empty_execution_result",
    )

    log_path = data_dir / "dashboard" / "agent_execution.log.jsonl"
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    result = rows[-1]["execution_result"]
    assert result["failure_classification"] == "no_result"
    assert result["trace_summary"]["failure_detail"] == "empty_execution_result"

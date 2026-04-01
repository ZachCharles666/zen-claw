import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from zen_claw.agent.intent_router import IntentRouteResult
from zen_claw.agent.intent_router_contracts import IntentArbitrationResult
from zen_claw.agent.loop import AgentLoop
from zen_claw.bus.queue import MessageBus
from zen_claw.dashboard.server import build_dashboard_snapshot
from zen_claw.providers.base import LLMProvider, LLMResponse


class _FailIfCalledProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key=None, api_base=None)

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        raise AssertionError("LLM chat should not be called for direct dashboard trace test")

    def get_default_model(self) -> str:
        return "fake-model"


class _QueueProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__(api_key=None, api_base=None)
        self._responses = list(responses)
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.calls += 1
        if self._responses:
            return self._responses.pop(0)
        return LLMResponse(content="done", tool_calls=[])

    def get_default_model(self) -> str:
        return "fake-model"


def test_process_direct_appends_intent_router_dashboard_event(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)

    loop = AgentLoop(
        bus=MessageBus(),
        provider=_FailIfCalledProvider(),
        workspace=tmp_path,
        model="fake-model",
        enable_planning=False,
    )
    loop.sessions.sessions_dir = tmp_path / "sessions"
    loop.sessions.sessions_dir.mkdir(parents=True, exist_ok=True)
    loop._extract_and_store_memory = AsyncMock()  # type: ignore[method-assign]

    out = asyncio.run(loop.process_direct("请告诉我纽约现在几点"))

    assert "纽约当前时间：" in out
    log_path = data_dir / "dashboard" / "intent_router.log.jsonl"
    assert log_path.exists()
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    assert rows[-1]["intent_name"] == "time"
    assert rows[-1]["route_status"] == "direct_success"
    assert rows[-1]["handled"] is True
    execution_log_path = data_dir / "dashboard" / "agent_execution.log.jsonl"
    execution_rows = [
        json.loads(line) for line in execution_log_path.read_text(encoding="utf-8").splitlines() if line
    ]
    assert execution_rows[-1]["routing_decision"]["action"] == "execute"
    assert execution_rows[-1]["execution_intent"]["path_type"] == "direct"
    assert execution_rows[-1]["execution_result"]["status"] == "succeeded"


def test_build_dashboard_snapshot_includes_intent_router_events(
    monkeypatch, tmp_path: Path
) -> None:
    from zen_claw.config.schema import Config

    cfg = Config()
    data_dir = tmp_path / "data"
    (data_dir / "cron").mkdir(parents=True, exist_ok=True)
    (data_dir / "channels").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes").mkdir(parents=True, exist_ok=True)
    (data_dir / "dashboard").mkdir(parents=True, exist_ok=True)
    (data_dir / "cron" / "jobs.json").write_text('{"jobs":[]}', encoding="utf-8")
    (data_dir / "channels" / "rate_limit_stats.json").write_text(
        '{"channels":{}}', encoding="utf-8"
    )
    (data_dir / "nodes" / "state.json").write_text(
        '{"version":1,"nodes":{},"tasks":[],"approval_events":[]}',
        encoding="utf-8",
    )
    (data_dir / "dashboard" / "intent_router.log.jsonl").write_text(
        json.dumps(
            {
                "at_ms": 123,
                "trace_id": "trace-1",
                "channel": "cli",
                "chat_id": "direct",
                "intent_name": "weather",
                "route_status": "direct_failed",
                "handled": True,
                "diagnostic": "weather_days_exceed_limit:70",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(
        "zen_claw.runtime.sidecar_supervisor.collect_sidecar_status", lambda _cfg: []
    )

    snapshot = build_dashboard_snapshot(cfg)

    assert len(snapshot["agent"]["intent_router_events"]) == 1
    assert snapshot["agent"]["intent_router_events"][0]["intent_name"] == "weather"
    assert snapshot["agent"]["intent_router_events"][0]["route_status"] == "direct_failed"
    assert snapshot["agent"]["intent_router_summary"]["total"] == 1
    assert snapshot["agent"]["intent_router_summary"]["direct_failed"] == 1
    assert snapshot["agent"]["intent_router_summary"]["direct_success"] == 0


def test_append_intent_router_event_persists_gate_migration_fields(
    monkeypatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)

    loop = AgentLoop(
        bus=MessageBus(),
        provider=_FailIfCalledProvider(),
        workspace=tmp_path,
        model="fake-model",
        enable_planning=False,
    )

    class _Msg:
        channel = "cli"
        chat_id = "direct"

    route_result = IntentRouteResult(
        handled=True,
        intent_name="daily_workflow",
        route_status="needs_constrained_replan",
        diagnostic="gate_migration_test",
        route_candidate={
            "match": "daily_workflow",
            "source": "declarative",
            "raw_confidence": 0.72,
            "crystallized_route": "",
        },
        delegate_reason="safety_valve_low_confidence",
        safety_valve_outcome="delegate",
        arbitration_result="skill_selected",
        safety_valve_trace={
            "weighted_confidence": 0.72,
            "final_confidence": 0.57,
            "residual_ratio": 0.48,
            "matched_candidates_count": 1,
            "has_context_pronoun": False,
            "length_outlier": True,
        },
    )

    loop._append_intent_router_event(
        trace_id="trace-gate-fields",
        msg=_Msg(),
        route_result=route_result,
    )

    log_path = data_dir / "dashboard" / "intent_router.log.jsonl"
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    assert rows[-1]["intent_name"] == "daily_workflow"
    assert rows[-1]["route_status"] == "needs_constrained_replan"
    assert rows[-1]["delegate_reason"] == "safety_valve_low_confidence"
    assert rows[-1]["safety_valve_outcome"] == "delegate"
    assert rows[-1]["arbitration_result"] == "skill_selected"
    assert rows[-1]["routing_stage"] == "gate2_delegate"
    assert rows[-1]["entered_gate3"] is False
    assert rows[-1]["route_candidate_match"] == "daily_workflow"
    assert rows[-1]["route_candidate_source"] == "declarative"
    assert rows[-1]["route_candidate_raw_confidence"] == 0.72
    assert rows[-1]["route_candidate_crystallized_route"] == ""
    assert rows[-1]["safety_valve_weighted_confidence"] == 0.72
    assert rows[-1]["safety_valve_final_confidence"] == 0.57
    assert rows[-1]["safety_valve_residual_ratio"] == 0.48
    assert rows[-1]["safety_valve_matched_candidates_count"] == 1
    assert rows[-1]["safety_valve_has_context_pronoun"] is False
    assert rows[-1]["safety_valve_length_outlier"] is True


@pytest.mark.asyncio
async def test_process_direct_persists_rule_history_and_safety_valve_delegate_trace(
    tmp_path: Path, monkeypatch
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)

    provider = _QueueProvider([LLMResponse(content="agent fallback", tool_calls=[])])
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="fake-model",
        enable_planning=False,
    )
    loop.sessions.sessions_dir = tmp_path / "sessions"
    loop.sessions.sessions_dir.mkdir(parents=True, exist_ok=True)
    loop._extract_and_store_memory = AsyncMock()  # type: ignore[method-assign]

    tool_result = MagicMock()
    tool_result.ok = True
    tool_result.content = "You have 3 unread emails."
    tool_result.error = None
    loop.tools.execute = AsyncMock(return_value=tool_result)  # type: ignore[method-assign]

    first = await loop.process_direct("未读邮件")
    assert "3 unread emails" in first
    session = loop.sessions.get_or_create("cli:direct")
    assert session.metadata["intent_router_history"]["email_check"]["success"] == 1
    assert session.metadata["intent_router_history"]["email_check"]["failure"] == 0
    assert session.metadata["intent_router_last_rule"]["intent_name"] == "email_check"
    assert session.metadata["intent_router_last_rule"]["source"] == "declarative"

    session.metadata["intent_router_history"]["email_check"]["success"] = 0
    session.metadata["intent_router_history"]["email_check"]["failure"] = 1
    loop.intent_router_classifier.classify = AsyncMock(  # type: ignore[method-assign]
        return_value=IntentArbitrationResult(kind="unclassified", diagnostic="test_unclassified")
    )
    loop._enter_gate3_agent_loop = AsyncMock(  # type: ignore[method-assign]
        return_value=("agent fallback", {"prompt_tokens": 1})
    )
    second = await loop.process_direct("未读邮件")

    assert second == "agent fallback"
    assert provider.calls == 0
    log_path = data_dir / "dashboard" / "intent_router.log.jsonl"
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    assert rows[-1]["intent_name"] == "email_check"
    assert rows[-1]["route_status"] == "needs_constrained_replan"
    assert rows[-1]["delegate_reason"] == "safety_valve_low_confidence"
    assert rows[-1]["safety_valve_outcome"] == "delegate"
    assert rows[-1]["arbitration_result"] == "unclassified"
    assert rows[-1]["routing_stage"] == "gate3_plan"
    assert rows[-1]["entered_gate3"] is True
    assert rows[-1]["route_candidate_match"] == "email_check"
    assert rows[-1]["route_candidate_source"] == "declarative"
    assert rows[-1]["route_candidate_raw_confidence"] == 0.9

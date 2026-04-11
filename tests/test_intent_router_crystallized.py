"""Tests for crystallized Gate 1 candidate routing."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from zen_claw.agent.intent_router import IntentRouter
from zen_claw.agent.intent_router_crystallized import CrystallizedIntent, CrystallizedIntentEngine
from zen_claw.agent.loop import AgentLoop
from zen_claw.bus.queue import MessageBus
from zen_claw.providers.base import LLMProvider


def _make_tools_mock(tool_result_content: str = "ok", ok: bool = True) -> MagicMock:
    mock = MagicMock()
    result = MagicMock()
    result.ok = ok
    result.content = tool_result_content
    result.error = None if ok else MagicMock(message="test error", retryable=False)
    mock.execute = AsyncMock(return_value=result)
    return mock


class _FailIfCalledProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key=None, api_base=None)

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        raise AssertionError("LLM chat should not be called for crystallized direct test")

    def get_default_model(self) -> str:
        return "fake-model"


@pytest.mark.asyncio
async def test_router_prefers_crystallized_candidate_before_declarative() -> None:
    router = IntentRouter()
    tools = _make_tools_mock("You have 2 unread emails.")

    result = await router.route(
        "帮我看看收件箱",
        tools=tools,
        trace_id="trace-crystallized-direct",
    )

    assert result.handled is True
    assert result.intent_name == "email_check"
    assert result.route_status == "direct_success"
    assert result.route_candidate is not None
    assert result.route_candidate["source"] == "crystallized"
    assert result.route_candidate["match"] == "email_check"
    assert result.route_candidate["crystallized_route"] == "inbox_triage_promoted"
    assert result.route_candidate["raw_confidence"] == 0.97


def test_crystallized_engine_skips_retired_route() -> None:
    engine = CrystallizedIntentEngine()
    engine.register(
        CrystallizedIntent(
            name="retired_email",
            target_intent_name="email_check",
            patterns=[r"收件箱"],
            max_consecutive_failures=2,
        )
    )

    matched = engine.match(
        "收件箱",
        control_context={
            "intent_router_crystallized_state": {
                "retired_email": {"consecutive_failures": 2}
            }
        },
    )

    assert matched is None


@pytest.mark.asyncio
async def test_process_direct_retires_crystallized_route_after_failures(
    tmp_path: Path,
    monkeypatch,
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
    loop.sessions.sessions_dir = tmp_path / "sessions"
    loop.sessions.sessions_dir.mkdir(parents=True, exist_ok=True)
    loop._extract_and_store_memory = AsyncMock()  # type: ignore[method-assign]

    tool_result_fail = MagicMock()
    tool_result_fail.ok = False
    tool_result_fail.content = ""
    tool_result_fail.error = MagicMock(message="mail backend failed", retryable=False)
    tool_result_ok = MagicMock()
    tool_result_ok.ok = True
    tool_result_ok.content = "You have 1 unread email."
    tool_result_ok.error = None
    loop.tools.execute = AsyncMock(side_effect=[tool_result_fail, tool_result_fail, tool_result_ok])  # type: ignore[method-assign]

    first = await loop.process_direct("帮我看看收件箱", channel="cli", chat_id="direct")
    session = loop.sessions.get_or_create("cli:direct")
    # Reset both lifetime and recency counters (recency fields added in Workstream 2).
    session.metadata["intent_router_history"]["email_check"]["success"] = 1
    session.metadata["intent_router_history"]["email_check"]["failure"] = 0
    session.metadata["intent_router_history"]["email_check"]["recent_success"] = 1
    session.metadata["intent_router_history"]["email_check"]["recent_failure"] = 0
    second = await loop.process_direct("帮我看看收件箱", channel="cli", chat_id="direct")
    session.metadata["intent_router_history"]["email_check"]["success"] = 1
    session.metadata["intent_router_history"]["email_check"]["failure"] = 0
    session.metadata["intent_router_history"]["email_check"]["recent_success"] = 1
    session.metadata["intent_router_history"]["email_check"]["recent_failure"] = 0
    third = await loop.process_direct("帮我看看收件箱", channel="cli", chat_id="direct")

    assert "mail backend failed" in first
    assert "mail backend failed" in second
    assert "1 unread email" in third

    state = session.metadata["intent_router_crystallized_state"]["inbox_triage_promoted"]
    assert state["failure"] == 2
    assert state["consecutive_failures"] == 2

    log_path = data_dir / "dashboard" / "intent_router.log.jsonl"
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    assert rows[0]["route_candidate_source"] == "crystallized"
    assert rows[0]["route_candidate_crystallized_route"] == "inbox_triage_promoted"
    assert rows[1]["route_candidate_source"] == "crystallized"
    assert rows[2]["route_candidate_source"] == "declarative"
    assert rows[2]["route_candidate_crystallized_route"] == ""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import zen_claw.agent.loop as _loop_module
from zen_claw.agent.intent_router import IntentRouteResult, IntentToolContract
from zen_claw.agent.intent_router_classifier import IntentRouterClassifier
from zen_claw.agent.intent_router_contracts import IntentArbitrationResult
from zen_claw.agent.loop import AgentLoop
from zen_claw.bus.queue import MessageBus
from zen_claw.providers.base import LLMProvider, LLMResponse


@dataclass
class _Session:
    key: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        self.messages.append({"role": role, "content": content, **kwargs})

    def get_history(self, max_messages: int = 50) -> list[dict[str, Any]]:
        recent = self.messages[-max_messages:]
        return [{"role": m["role"], "content": m["content"]} for m in recent]


class _InMemorySessionManager:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self._cache: dict[str, _Session] = {}

    def get_or_create(self, key: str) -> _Session:
        if key not in self._cache:
            self._cache[key] = _Session(key=key)
        return self._cache[key]

    def save(self, session: _Session) -> None:
        self._cache[session.key] = session


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


def _make_loop(tmp_path: Path, provider: LLMProvider) -> AgentLoop:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="fake-model",
        enable_planning=False,
        max_iterations=2,
    )
    loop.sessions = _InMemorySessionManager(tmp_path)
    loop._extract_and_store_memory = AsyncMock()  # type: ignore[method-assign]
    return loop


def _delegate_route_result() -> IntentRouteResult:
    return IntentRouteResult(
        handled=True,
        intent_name="email_check",
        content="未读邮件",
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
    )


def test_classifier_parses_confirm_candidate_json() -> None:
    provider = _QueueProvider(
        [
            LLMResponse(
                content='{"kind":"confirm_candidate","candidate_name":"email_check","diagnostic":"ok"}',
                tool_calls=[],
            )
        ]
    )
    classifier = IntentRouterClassifier()

    result = asyncio.run(
        classifier.classify(
            provider=provider,
            model="fake-model",
            content="未读邮件",
            route_candidate={"match": "email_check", "source": "declarative"},
            delegate_reason="safety_valve_low_confidence",
            available_skills=["daily_assistant"],
        )
    )

    assert result.kind == "confirm_candidate"
    assert result.candidate_name == "email_check"


def test_classifier_invalid_payload_degrades_to_unclassified() -> None:
    provider = _QueueProvider([LLMResponse(content="not json", tool_calls=[])])
    classifier = IntentRouterClassifier()

    result = asyncio.run(
        classifier.classify(
            provider=provider,
            model="fake-model",
            content="未读邮件",
            route_candidate={"match": "email_check", "source": "declarative"},
            available_skills=["daily_assistant"],
        )
    )

    assert result.kind == "unclassified"
    assert result.diagnostic == "classifier_invalid_payload"


def test_classifier_prompt_mentions_clarification_vs_unclassified_boundary() -> None:
    messages = IntentRouterClassifier._build_messages(
        content="帮我分析这两个方案",
        history=[],
        route_candidate=None,
        delegate_reason="safety_valve_low_confidence",
        available_skills=["daily_assistant"],
    )

    assert "Use request_clarification only when the user request is missing a key object" in messages[0]["content"]
    assert "return unclassified instead of asking a clarification" in messages[0]["content"]
    assert "\"examples\"" in messages[1]["content"]


def test_gate2_request_clarification_returns_early(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(_loop_module, "SessionManager", _InMemorySessionManager)
    loop = _make_loop(tmp_path, _QueueProvider([]))

    async def _fake_route(_content: str, *, tools, trace_id: str):
        _ = tools, trace_id
        return _delegate_route_result()

    loop.intent_router_classifier.classify = AsyncMock(  # type: ignore[method-assign]
        return_value=IntentArbitrationResult(
            kind="request_clarification",
            clarification_question="你是要查未读邮件，还是搜索某个发件人的邮件？",
        )
    )
    monkeypatch.setattr(loop.intent_router, "route", _fake_route)
    loop._run_execute_reflect_loop = AsyncMock()  # type: ignore[method-assign]
    loop._run_plan_phase = AsyncMock()  # type: ignore[method-assign]
    loop._enter_gate3_agent_loop = AsyncMock()  # type: ignore[method-assign]

    out = asyncio.run(loop.process_direct("不是这个意思", channel="cli", chat_id="direct"))

    assert out == "你是要查未读邮件，还是搜索某个发件人的邮件？"
    loop._run_execute_reflect_loop.assert_not_awaited()
    loop._run_plan_phase.assert_not_awaited()
    loop._enter_gate3_agent_loop.assert_not_awaited()


def test_gate2_confirm_candidate_does_not_enter_gate3(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(_loop_module, "SessionManager", _InMemorySessionManager)
    provider = _QueueProvider([LLMResponse(content="confirmed answer", tool_calls=[])])
    loop = _make_loop(tmp_path, provider)

    async def _fake_route(_content: str, *, tools, trace_id: str):
        _ = tools, trace_id
        return _delegate_route_result()

    loop.intent_router_classifier.classify = AsyncMock(  # type: ignore[method-assign]
        return_value=IntentArbitrationResult(kind="confirm_candidate", candidate_name="email_check")
    )
    monkeypatch.setattr(loop.intent_router, "route", _fake_route)
    loop._enter_gate3_agent_loop = AsyncMock()  # type: ignore[method-assign]

    out = asyncio.run(loop.process_direct("未读邮件", channel="cli", chat_id="direct"))

    assert out == "confirmed answer"
    loop._enter_gate3_agent_loop.assert_not_awaited()


def test_gate2_select_skill_does_not_enter_gate3(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(_loop_module, "SessionManager", _InMemorySessionManager)
    provider = _QueueProvider([LLMResponse(content="skill biased answer", tool_calls=[])])
    loop = _make_loop(tmp_path, provider)
    loop.skill_names = ["daily_assistant"]

    async def _fake_route(_content: str, *, tools, trace_id: str):
        _ = tools, trace_id
        return _delegate_route_result()

    loop.intent_router_classifier.classify = AsyncMock(  # type: ignore[method-assign]
        return_value=IntentArbitrationResult(kind="select_skill", skill_name="daily_assistant")
    )
    monkeypatch.setattr(loop.intent_router, "route", _fake_route)
    loop._enter_gate3_agent_loop = AsyncMock()  # type: ignore[method-assign]

    out = asyncio.run(loop.process_direct("未读邮件", channel="cli", chat_id="direct"))

    assert out == "skill biased answer"
    loop._enter_gate3_agent_loop.assert_not_awaited()


def test_gate2_unclassified_falls_back_to_normal_llm_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(_loop_module, "SessionManager", _InMemorySessionManager)
    provider = _QueueProvider([LLMResponse(content="fallback answer", tool_calls=[])])
    loop = _make_loop(tmp_path, provider)

    async def _fake_route(_content: str, *, tools, trace_id: str):
        _ = tools, trace_id
        return _delegate_route_result()

    loop.intent_router_classifier.classify = AsyncMock(  # type: ignore[method-assign]
        return_value=IntentArbitrationResult(kind="unclassified", diagnostic="still_unclear")
    )
    monkeypatch.setattr(loop.intent_router, "route", _fake_route)
    observed: dict[str, Any] = {}

    async def _fake_gate3(**kwargs: Any):
        observed["route_result"] = kwargs["route_result"]
        return "fallback answer", {"prompt_tokens": 1}

    loop._enter_gate3_agent_loop = AsyncMock(side_effect=_fake_gate3)  # type: ignore[method-assign]

    out = asyncio.run(loop.process_direct("未读邮件", channel="cli", chat_id="direct"))

    assert out == "fallback answer"
    assert provider.calls == 0
    loop._enter_gate3_agent_loop.assert_awaited_once()
    assert observed["route_result"].arbitration_result == "unclassified"
    assert observed["route_result"].contract is not None
    assert observed["route_result"].contract.intent_name == "default_contract"
    assert "exec" in observed["route_result"].contract.denied_tools

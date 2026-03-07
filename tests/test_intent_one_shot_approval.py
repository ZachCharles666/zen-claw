from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from zen_claw.agent.approval_gate import ApprovalGate
from zen_claw.agent.intent_router import IntentRouteResult, IntentToolContract
from zen_claw.agent.loop import AgentLoop
from zen_claw.agent.skills import SkillsLoader
from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.result import ToolResult
from zen_claw.bus.queue import MessageBus
from zen_claw.providers.base import LLMProvider, LLMResponse, ToolCallRequest


@pytest.fixture(autouse=True)
def mock_skills_loader(monkeypatch):
    monkeypatch.setattr(SkillsLoader, "_load_skill_mapping", lambda self: None)
    monkeypatch.setattr(SkillsLoader, "_save_skill_mapping", lambda self: None)
    monkeypatch.setattr(SkillsLoader, "_now_ts", lambda self: 1000.0)


class _QueueProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__(api_key=None, api_base=None)
        self._responses = list(responses)
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.calls += 1
        if not self._responses:
            raise AssertionError("provider.chat called unexpectedly")
        return self._responses.pop(0)

    def get_default_model(self) -> str:
        return "fake-model"


class _FakeExecTool(Tool):
    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return "fake exec"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        }

    async def execute(self, command: str = "", **kwargs):
        return ToolResult.success(f"executed:{command}")


def _make_route_result() -> IntentRouteResult:
    return IntentRouteResult(
        handled=True,
        intent_name="code_exec",
        content="当前安全路径无法完成，需要一次性显式授权才能继续。",
        contract=IntentToolContract(
            intent_name="code_exec",
            preferred_tools=["web_fetch"],
            allowed_tools={"web_fetch"},
            denied_tools={"exec", "spawn"},
            allow_constrained_replan=True,
            allow_high_risk_escalation=True,
            response_mode="llm_assisted",
        ),
        route_status="needs_explicit_approval",
        diagnostic="explicit_approval:exec",
    )


def _make_low_risk_route_result() -> IntentRouteResult:
    return IntentRouteResult(
        handled=True,
        intent_name="weather_like",
        content=None,
        contract=IntentToolContract(
            intent_name="weather_like",
            preferred_tools=["web_fetch"],
            allowed_tools={"web_fetch"},
            denied_tools={"exec", "spawn", "write_file", "edit_file"},
            allow_constrained_replan=True,
            allow_high_risk_escalation=False,
            response_mode="llm_assisted",
        ),
        route_status="needs_constrained_replan",
        diagnostic="weather_sources_failed:wttr,open_meteo",
    )


def _make_loop(tmp_path: Path, provider: LLMProvider) -> AgentLoop:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="fake-model",
        enable_planning=False,
    )
    loop.sessions.sessions_dir = tmp_path / "sessions"
    loop.sessions.sessions_dir.mkdir(parents=True, exist_ok=True)
    loop.approval_gate = ApprovalGate(tmp_path)
    loop._extract_and_store_memory = AsyncMock()  # type: ignore[method-assign]
    return loop


@pytest.mark.asyncio
async def test_one_shot_explicit_approval_requests_minimal_scope(tmp_path: Path, monkeypatch) -> None:
    provider = _QueueProvider([])
    loop = _make_loop(tmp_path, provider)

    async def _fake_route(content: str, *, tools, trace_id: str):
        return _make_route_result()

    monkeypatch.setattr(loop.intent_router, "route", _fake_route)

    out = await loop.process_direct("run dangerous task", channel="cli", chat_id="direct")

    pending = loop.approval_gate.list_pending(session_id="cli:direct")
    assert len(pending) == 1
    assert pending[0].tool_name == "intent_one_shot_approval"
    assert pending[0].tool_args == {"intent": "code_exec", "approved_tools": ["exec"]}
    assert "/approve" in out
    assert "intent_one_shot_approval" in out
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_one_shot_explicit_approval_allows_sensitive_tool_for_one_turn(
    tmp_path: Path, monkeypatch
) -> None:
    provider = _QueueProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="tc-1", name="exec", arguments={"command": "echo hi"})],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="done", tool_calls=[]),
        ]
    )
    loop = _make_loop(tmp_path, provider)
    loop.tools.unregister("exec")
    loop.tools.register(_FakeExecTool())

    async def _fake_route(content: str, *, tools, trace_id: str):
        return _make_route_result()

    monkeypatch.setattr(loop.intent_router, "route", _fake_route)

    first = await loop.process_direct("run dangerous task", channel="cli", chat_id="direct")
    pending = loop.approval_gate.list_pending(session_id="cli:direct")
    assert len(pending) == 1
    approved = loop.approval_gate.approve(pending[0].approval_id)
    assert approved is not None

    second = await loop.process_direct("run dangerous task", channel="cli", chat_id="direct")

    assert "/approve" in first
    assert second == "done"
    assert provider.calls == 2
    assert loop.approval_gate.list_pending(session_id="cli:direct") == []


@pytest.mark.asyncio
async def test_contract_denied_sensitive_tool_does_not_become_approval_candidate(
    tmp_path: Path, monkeypatch
) -> None:
    provider = _QueueProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="tc-1", name="exec", arguments={"command": "echo hi"})],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="this requires approval", tool_calls=[]),
        ]
    )
    loop = _make_loop(tmp_path, provider)

    async def _fake_route(content: str, *, tools, trace_id: str):
        return _make_low_risk_route_result()

    monkeypatch.setattr(loop.intent_router, "route", _fake_route)

    out = await loop.process_direct("tell me weather safely", channel="cli", chat_id="direct")

    assert out == "当前安全路径执行未成功，这次失败不是权限或审批问题。请稍后重试。"
    assert provider.calls == 2
    assert loop.approval_gate.list_pending(session_id="cli:direct") == []

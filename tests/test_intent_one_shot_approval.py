import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from zen_claw.agent.approval_gate import ApprovalGate
from zen_claw.agent.intent_router import (
    IntentRouter,
    IntentRouteResult,
    IntentToolContract,
    RecoveryBlocker,
    RecoveryOutcome,
    RecoveryPlan,
    RecoveryStrategy,
)
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
    plan = RecoveryPlan(
        blocker=RecoveryBlocker(
            kind="permission_required",
            description="当前安全路径缺少执行所需的高风险工具授权",
            missing_requirement="一次性显式授权的敏感工具范围",
        ),
        strategies=[
            RecoveryStrategy(kind="guidance_only", detail="先请求一次性显式授权再继续"),
        ],
        checked_scope=["已在当前安全路径内评估允许工具集合"],
        next_steps=["如果你确认，我会按最小工具范围发起一次性授权"],
        fallback_options=[],
    )
    return IntentRouter._needs_explicit_approval_with_plan(
        intent_name="code_exec",
        summary="当前安全路径无法完成，需要一次性显式授权才能继续。",
        contract=IntentToolContract(
            intent_name="code_exec",
            preferred_tools=["web_fetch"],
            allowed_tools={"web_fetch"},
            denied_tools={"exec", "spawn"},
            allow_constrained_replan=True,
            allow_high_risk_escalation=True,
            response_mode="llm_assisted",
        ),
        plan=plan,
        diagnostic="explicit_approval:exec",
    )


def _make_low_risk_route_result() -> IntentRouteResult:
    return IntentRouter._needs_constrained_replan(
        intent_name="weather_like",
        contract=IntentToolContract(
            intent_name="weather_like",
            preferred_tools=["web_fetch"],
            allowed_tools={"web_fetch"},
            denied_tools={"exec", "spawn", "write_file", "edit_file"},
            allow_constrained_replan=True,
            allow_high_risk_escalation=False,
            response_mode="llm_assisted",
        ),
        diagnostic="weather_sources_failed:wttr,open_meteo",
        recovery_outcome=RecoveryOutcome(
            mode="guided",
            content=(
                "暂时无法获取天气数据。当前卡点不是权限或审批问题，而是安全路径下的低风险数据来源"
                "暂时没有返回有效结果。下一步可继续这样处理：只继续尝试允许的网络抓取路径。"
            ),
            plan=RecoveryPlan(
                blocker=RecoveryBlocker(
                    kind="upstream_unavailable",
                    description="安全路径下的低风险数据来源暂时未返回有效结果",
                    missing_requirement="至少一个可访问且返回有效结果的低风险来源",
                ),
                strategies=[
                    RecoveryStrategy(
                        kind="fallback_source",
                        detail="继续尝试允许列表内的低风险数据源",
                    )
                ],
                checked_scope=["已限制在 web_fetch 安全路径内重试"],
                next_steps=["只继续尝试允许的网络抓取路径"],
                fallback_options=[],
            ),
        ),
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
async def test_real_exec_intent_requests_one_shot_approval_with_structured_guidance(
    tmp_path: Path,
) -> None:
    provider = _QueueProvider([])
    loop = _make_loop(tmp_path, provider)

    out = await loop.process_direct("执行命令 echo hi", channel="cli", chat_id="direct")

    pending = loop.approval_gate.list_pending(session_id="cli:direct")
    assert len(pending) == 1
    assert pending[0].tool_name == "intent_one_shot_approval"
    assert pending[0].tool_args == {"intent": "code_exec", "approved_tools": ["exec"]}
    assert "当前安全路径无法直接执行这条命令" in out
    assert "缺的是一次性显式授权的 exec 工具范围" in out
    assert "/approve" in out
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
async def test_real_exec_intent_runs_after_one_shot_approval(
    tmp_path: Path,
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

    first = await loop.process_direct("run command echo hi", channel="cli", chat_id="direct")
    pending = loop.approval_gate.list_pending(session_id="cli:direct")
    assert len(pending) == 1
    approved = loop.approval_gate.approve(pending[0].approval_id)
    assert approved is not None

    second = await loop.process_direct("run command echo hi", channel="cli", chat_id="direct")

    assert "/approve" in first
    assert second == "done"
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_one_shot_approval_conversion_appends_constrained_replan_trace_event(
    tmp_path: Path, monkeypatch
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)

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

    first = await loop.process_direct("run command echo hi", channel="cli", chat_id="direct")
    pending = loop.approval_gate.list_pending(session_id="cli:direct")
    assert len(pending) == 1
    approved = loop.approval_gate.approve(pending[0].approval_id)
    assert approved is not None

    second = await loop.process_direct("run command echo hi", channel="cli", chat_id="direct")

    assert "/approve" in first
    assert second == "done"
    rows = [
        json.loads(line)
        for line in (data_dir / "dashboard" / "intent_router.log.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        if line
    ]
    assert rows[-2]["route_status"] == "needs_explicit_approval"
    assert rows[-1]["route_status"] == "needs_constrained_replan"
    assert rows[-1]["recovery_mode"] == "guided"
    assert rows[-1]["recovery_blocker_kind"] == "permission_required"


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

    assert out.startswith("暂时无法获取天气数据。当前卡点不是权限或审批问题")
    assert provider.calls == 2
    assert loop.approval_gate.list_pending(session_id="cli:direct") == []


@pytest.mark.asyncio
async def test_explicit_approval_conversion_preserves_structured_recovery_into_replan_instruction(
    tmp_path: Path, monkeypatch
) -> None:
    provider = _QueueProvider(
        [
            LLMResponse(content="done", tool_calls=[]),
        ]
    )
    loop = _make_loop(tmp_path, provider)

    async def _fake_route(content: str, *, tools, trace_id: str):
        return _make_route_result()

    monkeypatch.setattr(loop.intent_router, "route", _fake_route)
    observed: dict[str, str] = {}

    def _capture_replan(route_result: IntentRouteResult) -> str:
        observed["route_status"] = route_result.route_status
        observed["recovery_mode"] = (
            route_result.recovery_outcome.mode if route_result.recovery_outcome is not None else ""
        )
        observed["missing_requirement"] = (
            route_result.recovery_outcome.plan.blocker.missing_requirement
            if route_result.recovery_outcome is not None and route_result.recovery_outcome.plan is not None
            else ""
        )
        return "captured constrained replan"

    monkeypatch.setattr(loop, "_build_intent_replan_instruction", _capture_replan)

    first = await loop.process_direct("run dangerous task", channel="cli", chat_id="direct")
    pending = loop.approval_gate.list_pending(session_id="cli:direct")
    assert len(pending) == 1
    approved = loop.approval_gate.approve(pending[0].approval_id)
    assert approved is not None

    second = await loop.process_direct("run dangerous task", channel="cli", chat_id="direct")

    assert "/approve" in first
    assert second == "done"
    assert observed == {
        "route_status": "needs_constrained_replan",
        "recovery_mode": "guided",
        "missing_requirement": "一次性显式授权的敏感工具范围",
    }

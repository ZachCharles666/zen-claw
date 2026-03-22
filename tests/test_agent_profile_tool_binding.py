import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import zen_claw.agent.loop as _loop_module
from zen_claw.agent.loop import AgentLoop
from zen_claw.agent.skills import SkillsLoader
from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult
from zen_claw.bus.queue import MessageBus
from zen_claw.config.schema import ToolPolicyConfig
from zen_claw.providers.base import LLMProvider, LLMResponse, ToolCallRequest


@pytest.fixture(autouse=True)
def mock_skills_loader(monkeypatch):
    monkeypatch.setattr(SkillsLoader, "_load_skill_mapping", lambda self: None)
    monkeypatch.setattr(SkillsLoader, "_save_skill_mapping", lambda self: None)
    monkeypatch.setattr(SkillsLoader, "_now_ts", lambda self: 1000.0)


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


_loop_module.SessionManager = _InMemorySessionManager


class _TrackingProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]):
        super().__init__(api_key=None, api_base=None)
        self._responses = list(responses)
        self.tool_snapshots: list[list[str]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        _ = messages, model, max_tokens, temperature
        names = []
        for row in tools or []:
            fn = row.get("function", {}) if isinstance(row, dict) else {}
            names.append(str(fn.get("name") or ""))
        self.tool_snapshots.append(names)
        return self._responses.pop(0)

    def get_default_model(self) -> str:
        return "fake-model"


class _AllowedTool(Tool):
    name = "allowed_tool"
    description = "allowed"
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> ToolResult:
        _ = kwargs
        return ToolResult.success("allowed")


class _BlockedTool(Tool):
    name = "blocked_tool"
    description = "blocked"
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> ToolResult:
        _ = kwargs
        return ToolResult.failure(ToolErrorKind.RUNTIME, "should not run", code="unexpected")


def _make_loop(tmp_path: Path, provider: LLMProvider) -> AgentLoop:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="fake-model",
        tool_policy_config=ToolPolicyConfig(default_deny_tools=[]),
        enable_planning=False,
        profile_allowed_tools=["allowed_tool"],
    )
    loop.sessions = _InMemorySessionManager(tmp_path)
    loop.tools.register(_AllowedTool())
    loop.tools.register(_BlockedTool())
    return loop


def test_profile_tool_binding_hides_blocked_tools_from_llm(tmp_path: Path) -> None:
    provider = _TrackingProvider([LLMResponse(content="done", tool_calls=[])])
    loop = _make_loop(tmp_path, provider)

    out = asyncio.run(loop.process_direct("hello", channel="cli", chat_id="u1"))
    assert out == "done"
    assert "allowed_tool" in provider.tool_snapshots[0]
    assert "blocked_tool" not in provider.tool_snapshots[0]


def test_profile_tool_binding_blocks_execution_outside_allowlist(tmp_path: Path) -> None:
    provider = _TrackingProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="1", name="blocked_tool", arguments={})],
            ),
            LLMResponse(content="fallback", tool_calls=[]),
        ]
    )
    loop = _make_loop(tmp_path, provider)

    out = asyncio.run(loop.process_direct("hello", channel="cli", chat_id="u1"))
    assert out == "fallback"
    result = asyncio.run(loop.tools.execute("blocked_tool", {}, trace_id="t-blocked"))
    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.PERMISSION

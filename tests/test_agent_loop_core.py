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
    # Mock mapping and time to prevent potentially slow/hanging I/O or crypto in CI
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


class _QueueProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]):
        super().__init__(api_key=None, api_base=None)
        self._responses = list(responses)
        self.calls = 0
        self.message_history: list[list[dict[str, Any]]] = []
        self.tool_history: list[list[dict[str, Any]] | None] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        self.calls += 1
        self.message_history.append([dict(message) for message in messages])
        self.tool_history.append(tools)
        if self._responses:
            return self._responses.pop(0)
        return LLMResponse(content="done", tool_calls=[])

    def get_default_model(self) -> str:
        return "fake-model"


def _make_loop(
    tmp_path: Path,
    provider: LLMProvider,
    *,
    max_iterations: int = 5,
    policy: ToolPolicyConfig | None = None,
) -> AgentLoop:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="fake-model",
        max_iterations=max_iterations,
        tool_policy_config=policy or ToolPolicyConfig(),
        enable_planning=False,
    )
    loop.sessions = _InMemorySessionManager(tmp_path)
    return loop


def test_tool_call_dispatch_reaches_registered_tool(tmp_path: Path) -> None:
    calls: dict[str, str] = {}

    class _EchoTool(Tool):
        name = "echo_test"
        description = "Echo"
        parameters = {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }

        async def execute(self, text: str = "", **kwargs: Any) -> ToolResult:
            calls["text"] = text
            return ToolResult.success(text)

    provider = _QueueProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="1", name="echo_test", arguments={"text": "ok"})],
            ),
            LLMResponse(content="done", tool_calls=[]),
        ]
    )
    loop = _make_loop(tmp_path, provider)
    loop.tools.register(_EchoTool())

    out = asyncio.run(loop.process_direct("run", channel="cli", chat_id="u1"))
    assert out == "done"
    assert calls["text"] == "ok"


def test_tool_call_turn_preserves_reasoning_content(tmp_path: Path) -> None:
    class _EchoTool(Tool):
        name = "echo_reasoning"
        description = "Echo"
        parameters = {"type": "object", "properties": {}, "required": []}

        async def execute(self, **kwargs: Any) -> ToolResult:
            return ToolResult.success("tool result")

    provider = _QueueProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="1", name="echo_reasoning", arguments={})],
                provider_metadata={"reasoning_content": "hidden reasoning"},
            ),
            LLMResponse(content="done", tool_calls=[]),
        ]
    )
    loop = _make_loop(tmp_path, provider)
    loop.tools.register(_EchoTool())

    out = asyncio.run(loop.process_direct("run", channel="cli", chat_id="u1"))

    assert out == "done"
    assert provider.message_history[1][-2]["role"] == "assistant"
    assert provider.message_history[1][-2]["reasoning_content"] == "hidden reasoning"


def test_xbench_channel_bypasses_direct_intent_router(tmp_path: Path, monkeypatch) -> None:
    provider = _QueueProvider([LLMResponse(content="最终答案: ok", tool_calls=[])])
    loop = _make_loop(tmp_path, provider)

    async def _route_should_not_run(*_args, **_kwargs):
        raise AssertionError("xbench requests must bypass direct intent routing")

    monkeypatch.setattr(loop.intent_router, "route", _route_should_not_run)

    out = asyncio.run(
        loop.process_direct(
            "你是一个通用人工智能助手。最终答案:[你的答案]\n\n[问题]: 现在几点对应的复杂检索题",
            channel="xbench",
            chat_id="eval",
        )
    )

    assert out == "最终答案: ok"
    assert provider.calls >= 1


def test_plain_xbench_channel_bypasses_direct_intent_router(tmp_path: Path, monkeypatch) -> None:
    provider = _QueueProvider([LLMResponse(content="最终答案: ok", tool_calls=[])])
    loop = _make_loop(tmp_path, provider)

    async def _route_should_not_run(*_args, **_kwargs):
        raise AssertionError("xbench requests must bypass direct intent routing")

    monkeypatch.setattr(loop.intent_router, "route", _route_should_not_run)

    out = asyncio.run(
        loop.process_direct(
            "What was the Solana price on the day this happened?",
            channel="xbench",
            chat_id="eval",
        )
    )

    assert out == "最终答案: ok"


def test_xbench_empty_response_triggers_forced_synthesis(
    tmp_path: Path, monkeypatch
) -> None:
    provider = _QueueProvider(
        [
            LLMResponse(content=None, tool_calls=[]),
            LLMResponse(content="最终答案: synthesized", tool_calls=[]),
        ]
    )
    loop = _make_loop(
        tmp_path, provider, policy=ToolPolicyConfig(production_hardening=False)
    )

    async def _route_should_not_run(*_args, **_kwargs):
        raise AssertionError("xbench requests must bypass direct intent routing")

    monkeypatch.setattr(loop.intent_router, "route", _route_should_not_run)

    out = asyncio.run(
        loop.process_direct(
            "最终答案:[你的答案]\n\n[问题]: 空输出后必须合成",
            channel="xbench",
            chat_id="eval",
        )
    )

    assert out == "最终答案: synthesized"
    assert provider.calls >= 2
    assert "不要再调用任何工具" in provider.message_history[1][-1]["content"]
    assert "逐项核对题目里的每个约束" in provider.message_history[1][-1]["content"]
    assert "只有完全没有候选答案时才写无" in provider.message_history[1][-1]["content"]
    assert "不要截断" in provider.message_history[1][-1]["content"]


def test_xbench_search_followup_prefers_fetching_authoritative_pages(
    tmp_path: Path, monkeypatch
) -> None:
    class _SearchTool(Tool):
        name = "web_search"
        description = "Search"
        parameters = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }

        async def execute(self, query: str = "", **kwargs: Any) -> ToolResult:
            return ToolResult.success(f"result for {query}: https://example.test/source")

    provider = _QueueProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(id="1", name="web_search", arguments={"query": "topic"})
                ],
            ),
            LLMResponse(content="最终答案: ok", tool_calls=[]),
        ]
    )
    loop = _make_loop(
        tmp_path, provider, policy=ToolPolicyConfig(production_hardening=False)
    )
    loop.tools.register(_SearchTool())

    async def _route_should_not_run(*_args, **_kwargs):
        raise AssertionError("xbench requests must bypass direct intent routing")

    monkeypatch.setattr(loop.intent_router, "route", _route_should_not_run)

    out = asyncio.run(
        loop.process_direct(
            "最终答案:[你的答案]\n\n[问题]: 需要检索",
            channel="xbench",
            chat_id="eval",
        )
    )

    assert out == "最终答案: ok"
    followup = provider.message_history[1][-1]["content"]
    assert "优先调用 web_fetch" in followup
    second_tools = provider.tool_history[1] or []
    second_tool_names = [
        tool["function"]["name"]
        for tool in second_tools
        if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
    ]
    assert second_tool_names == ["web_fetch"]
    assert loop._last_execution_trace_summary["search_query_count"] == 1
    assert loop._last_execution_trace_summary["fetched_url_count"] == 0


def test_failed_web_fetches_are_recorded_in_trace_summary(
    tmp_path: Path, monkeypatch
) -> None:
    class _FetchTool(Tool):
        name = "web_fetch"
        description = "Fetch"
        parameters = {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        }

        async def execute(self, url: str = "", **kwargs: Any) -> ToolResult:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                "fetch failed",
                code="web_fetch_failed",
            )

    provider = _QueueProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="1",
                        name="web_fetch",
                        arguments={"url": "https://example.test/page"},
                    )
                ],
            ),
            LLMResponse(content="最终答案: fallback", tool_calls=[]),
        ]
    )
    loop = _make_loop(
        tmp_path, provider, policy=ToolPolicyConfig(production_hardening=False)
    )
    loop.tools.register(_FetchTool())

    async def _route_should_not_run(*_args, **_kwargs):
        raise AssertionError("xbench requests must bypass direct intent routing")

    monkeypatch.setattr(loop.intent_router, "route", _route_should_not_run)

    out = asyncio.run(
        loop.process_direct(
            "最终答案:[你的答案]\n\n[问题]: 需要读取网页",
            channel="xbench",
            chat_id="eval",
        )
    )

    assert out == "最终答案: fallback"
    assert loop._last_execution_trace_summary["failed_fetches"] == [
        {
            "url": "https://example.test/page",
            "code": "web_fetch_failed",
            "kind": "runtime",
        }
    ]


def test_non_xbench_empty_response_does_not_force_synthesis(tmp_path: Path) -> None:
    provider = _QueueProvider([LLMResponse(content=None, tool_calls=[])])
    loop = _make_loop(tmp_path, provider)

    out = asyncio.run(loop.process_direct("hello", channel="cli", chat_id="u1"))

    assert out == "I've completed processing but have no response to give."
    assert not any(
        "不要再调用任何工具" in str(message.get("content"))
        for history in provider.message_history
        for message in history
    )


def test_max_iterations_terminates_loop(tmp_path: Path) -> None:
    counter = {"n": 0}

    class _InfiniteTool(Tool):
        name = "loop_tool"
        description = "loop"
        parameters = {"type": "object", "properties": {}, "required": []}

        async def execute(self, **kwargs: Any) -> ToolResult:
            counter["n"] += 1
            return ToolResult.success("step")

    class _InfiniteProvider(LLMProvider):
        def __init__(self):
            super().__init__(api_key=None, api_base=None)

        async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
            return LLMResponse(
                content=None, tool_calls=[ToolCallRequest(id="i", name="loop_tool", arguments={})]
            )

        def get_default_model(self) -> str:
            return "fake-model"

    loop = _make_loop(tmp_path, _InfiniteProvider(), max_iterations=3)
    loop.tools.register(_InfiniteTool())
    asyncio.run(loop.process_direct("x", channel="cli", chat_id="u1"))
    assert counter["n"] <= 3


def test_kill_switch_blocks_tool_execution(tmp_path: Path) -> None:
    executed = {"flag": False}

    class _BlockedTool(Tool):
        name = "blocked_tool"
        description = "blocked"
        parameters = {"type": "object", "properties": {}, "required": []}

        async def execute(self, **kwargs: Any) -> ToolResult:
            executed["flag"] = True
            return ToolResult.success("should not run")

    provider = _QueueProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="1", name="blocked_tool", arguments={})],
            ),
            LLMResponse(content="ok", tool_calls=[]),
        ]
    )
    policy = ToolPolicyConfig(kill_switch_enabled=True, kill_switch_reason="test")
    loop = _make_loop(tmp_path, provider, policy=policy)
    loop.tools.register(_BlockedTool())
    asyncio.run(loop.process_direct("x", channel="cli", chat_id="u1"))
    assert executed["flag"] is False


def test_tool_failure_does_not_crash_and_loop_continues(tmp_path: Path) -> None:
    class _FailingTool(Tool):
        name = "failing_tool"
        description = "fail"
        parameters = {"type": "object", "properties": {}, "required": []}

        async def execute(self, **kwargs: Any) -> ToolResult:
            return ToolResult.failure(ToolErrorKind.RUNTIME, "boom", code="boom")

    provider = _QueueProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="1", name="failing_tool", arguments={})],
            ),
            LLMResponse(content="recovered", tool_calls=[]),
        ]
    )
    loop = _make_loop(tmp_path, provider)
    loop.tools.register(_FailingTool())
    out = asyncio.run(loop.process_direct("x", channel="cli", chat_id="u1"))
    assert out == "recovered"
    assert provider.calls >= 2


def test_unknown_tool_name_does_not_crash_loop(tmp_path: Path) -> None:
    provider = _QueueProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="1", name="missing_tool", arguments={})],
            ),
            LLMResponse(content="fallback", tool_calls=[]),
        ]
    )
    loop = _make_loop(tmp_path, provider)
    out = asyncio.run(loop.process_direct("x", channel="cli", chat_id="u1"))
    assert out == "fallback"
    assert provider.calls >= 1


def test_multiple_tool_calls_in_one_response_are_all_dispatched(tmp_path: Path) -> None:
    calls: list[str] = []

    class _LogTool(Tool):
        name = "log_tool"
        description = "log"
        parameters = {
            "type": "object",
            "properties": {"label": {"type": "string"}},
            "required": ["label"],
        }

        async def execute(self, label: str = "", **kwargs: Any) -> ToolResult:
            calls.append(label)
            return ToolResult.success(label)

    provider = _QueueProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(id="1", name="log_tool", arguments={"label": "A"}),
                    ToolCallRequest(id="2", name="log_tool", arguments={"label": "B"}),
                    ToolCallRequest(id="3", name="log_tool", arguments={"label": "C"}),
                ],
            ),
            LLMResponse(content="done", tool_calls=[]),
        ]
    )
    loop = _make_loop(tmp_path, provider)
    loop.tools.register(_LogTool())
    out = asyncio.run(loop.process_direct("x", channel="cli", chat_id="u1"))
    assert out == "done"
    assert calls == ["A", "B", "C"]

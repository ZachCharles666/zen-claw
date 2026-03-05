import asyncio
from pathlib import Path
from typing import Any

from zen_claw.agent.loop import AgentLoop
from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.policy import ToolPolicyEngine
from zen_claw.agent.tools.registry import ToolRegistry
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult
from zen_claw.bus.queue import MessageBus
from zen_claw.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class FakeProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]):
        super().__init__(api_key=None, api_base=None)
        self._responses = responses
        self.calls = 0
        self.models: list[str | None] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        self.calls += 1
        self.models.append(model)
        if self._responses:
            return self._responses.pop(0)
        return LLMResponse(content="done")

    def get_default_model(self) -> str:
        return "fake-model"


class FakeTools:
    def __init__(self, result: ToolResult):
        self._result = result
        self.calls = 0

    def get_definitions(self) -> list[dict[str, Any]]:
        return []

    async def execute(
        self, name: str, arguments: dict[str, Any], trace_id: str | None = None
    ) -> ToolResult:
        self.calls += 1
        return self._result


class DummyTool(Tool):
    @property
    def name(self) -> str:
        return "deny_tool"

    @property
    def description(self) -> str:
        return "dummy tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult.success("ok")


class DummySearchTool(Tool):
    @property
    def name(self) -> str:
        return "dummy_search"

    @property
    def description(self) -> str:
        return "dummy search tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1}},
            "required": ["query"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult.success(f"result for {kwargs.get('query', '')}")


def _build_loop(tmp_path: Path, provider: FakeProvider, monkeypatch) -> AgentLoop:
    class _NoopSessionManager:
        def __init__(self, workspace: Path):
            self.workspace = workspace

    monkeypatch.setattr("zen_claw.agent.loop.SessionManager", _NoopSessionManager)
    return AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="fake-model",
        max_iterations=4,
        brave_api_key=None,
        restrict_to_workspace=True,
    )


def test_run_plan_phase_injects_plan_message(tmp_path: Path, monkeypatch) -> None:
    provider = FakeProvider([LLMResponse(content="1) search 2) summarize")])
    loop = _build_loop(tmp_path, provider, monkeypatch)
    messages = [{"role": "user", "content": "help me"}]

    out = asyncio.run(loop._run_plan_phase(messages, "help me", "trace-1"))
    assert provider.calls == 1
    assert any(m["role"] == "assistant" and "[Plan]" in str(m.get("content")) for m in out)


def test_run_plan_phase_skips_when_planning_disabled(tmp_path: Path, monkeypatch) -> None:
    provider = FakeProvider([LLMResponse(content="should not be used")])
    loop = _build_loop(tmp_path, provider, monkeypatch)
    loop.execution.enable_planning = False
    messages = [{"role": "user", "content": "help me"}]

    out = asyncio.run(loop._run_plan_phase(messages, "help me", "trace-0"))
    assert out == messages
    assert provider.calls == 0


def test_plan_then_execute_flow_keeps_plan_message_and_completes(
    tmp_path: Path, monkeypatch
) -> None:
    provider = FakeProvider(
        [
            LLMResponse(content="1) call tool"),
            LLMResponse(
                content="tool step",
                tool_calls=[
                    ToolCallRequest(id="t1", name="dummy_search", arguments={"query": "x"})
                ],
            ),
            LLMResponse(content="final"),
        ]
    )
    loop = _build_loop(tmp_path, provider, monkeypatch)
    reg = ToolRegistry(policy=ToolPolicyEngine())
    reg.register(DummySearchTool())
    loop.tools = reg

    msgs = [{"role": "user", "content": "do task"}]
    planned = asyncio.run(loop._run_plan_phase(msgs, "do task", "trace-plan"))
    final, out = asyncio.run(loop._run_execute_reflect_loop(planned, "trace-plan"))
    assert final == "final"
    assert any(m.get("role") == "assistant" and "[Plan]" in str(m.get("content")) for m in planned)
    assert any(m.get("role") == "tool" and "result for x" in str(m.get("content")) for m in out)


def test_run_execute_reflect_loop_injects_reflection_prompt_on_error(
    tmp_path: Path, monkeypatch
) -> None:
    provider = FakeProvider(
        [
            LLMResponse(
                content="trying tool",
                tool_calls=[ToolCallRequest(id="t1", name="web_search", arguments={"q": "x"})],
            ),
            LLMResponse(content="final answer after reflection"),
        ]
    )
    loop = _build_loop(tmp_path, provider, monkeypatch)
    loop.tools = FakeTools(ToolResult.failure(ToolErrorKind.RETRYABLE, "timeout"))
    messages = [{"role": "user", "content": "find latest"}]

    final, out = asyncio.run(loop._run_execute_reflect_loop(messages, "trace-2"))
    assert final == "final answer after reflection"
    assert provider.calls == 2
    assert any(
        m["role"] == "user" and "Previous tool attempts failed" in str(m.get("content"))
        for m in out
    )


def test_run_execute_reflect_loop_skips_reflection_on_success(tmp_path: Path, monkeypatch) -> None:
    provider = FakeProvider(
        [
            LLMResponse(
                content="trying tool",
                tool_calls=[ToolCallRequest(id="t1", name="list_dir", arguments={"path": "."})],
            ),
            LLMResponse(content="done"),
        ]
    )
    loop = _build_loop(tmp_path, provider, monkeypatch)
    loop.tools = FakeTools(ToolResult.success("ok"))
    messages = [{"role": "user", "content": "list files"}]

    final, out = asyncio.run(loop._run_execute_reflect_loop(messages, "trace-3"))
    assert final == "done"
    assert provider.calls == 2
    assert not any(
        m["role"] == "user" and "Previous tool attempts failed" in str(m.get("content"))
        for m in out
    )


def test_run_execute_reflect_loop_respects_reflection_budget(tmp_path: Path, monkeypatch) -> None:
    provider = FakeProvider(
        [
            LLMResponse(
                content="first tool",
                tool_calls=[ToolCallRequest(id="t1", name="web_search", arguments={"q": "x"})],
            ),
            LLMResponse(
                content="second tool",
                tool_calls=[ToolCallRequest(id="t2", name="web_search", arguments={"q": "y"})],
            ),
        ]
    )
    loop = _build_loop(tmp_path, provider, monkeypatch)
    loop.max_iterations = 2
    loop.tools = FakeTools(ToolResult.failure(ToolErrorKind.RETRYABLE, "timeout"))
    messages = [{"role": "user", "content": "search twice"}]

    final, out = asyncio.run(loop._run_execute_reflect_loop(messages, "trace-4"))
    assert final is None
    # Reflection is injected only once due to max_reflections=1 default.
    reflected = [
        m
        for m in out
        if m.get("role") == "user" and "Previous tool attempts failed" in str(m.get("content"))
    ]
    assert len(reflected) == 1


def test_run_execute_reflect_loop_reflects_on_policy_denied_tool(
    tmp_path: Path, monkeypatch
) -> None:
    provider = FakeProvider(
        [
            LLMResponse(
                content="call denied tool",
                tool_calls=[ToolCallRequest(id="t1", name="deny_tool", arguments={})],
            ),
            LLMResponse(content="fallback response"),
        ]
    )
    loop = _build_loop(tmp_path, provider, monkeypatch)
    reg = ToolRegistry(policy=ToolPolicyEngine())
    reg.register(DummyTool())
    reg.set_policy_scope("agent", deny={"deny_tool"})
    loop.tools = reg
    messages = [{"role": "user", "content": "do denied thing"}]

    final, out = asyncio.run(loop._run_execute_reflect_loop(messages, "trace-5"))
    assert final == "fallback response"
    assert any(m.get("role") == "user" and "kind=permission" in str(m.get("content")) for m in out)


def test_run_execute_reflect_loop_parameter_error_then_recovery(
    tmp_path: Path, monkeypatch
) -> None:
    provider = FakeProvider(
        [
            LLMResponse(
                content="first try bad args",
                tool_calls=[ToolCallRequest(id="t1", name="dummy_search", arguments={})],
            ),
            LLMResponse(
                content="second try fixed args",
                tool_calls=[
                    ToolCallRequest(id="t2", name="dummy_search", arguments={"query": "openclaw"})
                ],
            ),
            LLMResponse(content="done after correction"),
        ]
    )
    loop = _build_loop(tmp_path, provider, monkeypatch)
    reg = ToolRegistry(policy=ToolPolicyEngine())
    reg.register(DummySearchTool())
    loop.tools = reg
    messages = [{"role": "user", "content": "search openclaw"}]

    final, out = asyncio.run(loop._run_execute_reflect_loop(messages, "trace-6"))
    assert final == "done after correction"
    # Ensure reflection for parameter error happened.
    assert any(m.get("role") == "user" and "kind=parameter" in str(m.get("content")) for m in out)
    # Ensure second tool result exists (successful corrected call).
    assert any(
        m.get("role") == "tool" and "result for openclaw" in str(m.get("content")) for m in out
    )
    learning_file = tmp_path / "memory" / "TOOLS_LEARNING.md"
    assert learning_file.exists()
    learning_text = learning_file.read_text(encoding="utf-8")
    assert "tool=dummy_search" in learning_text
    assert '"query": "openclaw"' in learning_text


def test_run_execute_reflect_loop_honors_max_reflections_config(
    tmp_path: Path, monkeypatch
) -> None:
    provider = FakeProvider(
        [
            LLMResponse(
                content="try 1",
                tool_calls=[ToolCallRequest(id="t1", name="deny_tool", arguments={})],
            ),
            LLMResponse(
                content="try 2",
                tool_calls=[ToolCallRequest(id="t2", name="deny_tool", arguments={})],
            ),
            LLMResponse(
                content="try 3",
                tool_calls=[ToolCallRequest(id="t3", name="deny_tool", arguments={})],
            ),
            LLMResponse(content="done after two reflections"),
        ]
    )
    loop = _build_loop(tmp_path, provider, monkeypatch)
    loop.max_iterations = 4
    loop.execution.max_reflections = 2
    reg = ToolRegistry(policy=ToolPolicyEngine())
    reg.register(DummyTool())
    reg.set_policy_scope("agent", deny={"deny_tool"})
    loop.tools = reg
    messages = [{"role": "user", "content": "denied action"}]

    final, out = asyncio.run(loop._run_execute_reflect_loop(messages, "trace-7"))
    assert final == "done after two reflections"
    reflected = [
        m
        for m in out
        if m.get("role") == "user" and "Previous tool attempts failed" in str(m.get("content"))
    ]
    assert len(reflected) == 2


def test_run_execute_reflect_loop_uses_vision_model_when_image_payload_present(
    tmp_path: Path, monkeypatch
) -> None:
    provider = FakeProvider([LLMResponse(content="vision final")])
    loop = _build_loop(tmp_path, provider, monkeypatch)
    loop.vision_model = "fake-vision-model"
    messages = [
        {"role": "system", "content": "sys"},
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                {"type": "text", "text": "what is this?"},
            ],
        },
    ]

    final, _ = asyncio.run(loop._run_execute_reflect_loop(messages, "trace-vision"))
    assert final == "vision final"
    assert provider.models[-1] == "fake-vision-model"


def test_run_execute_reflect_loop_keeps_default_model_without_image_payload(
    tmp_path: Path, monkeypatch
) -> None:
    provider = FakeProvider([LLMResponse(content="text final")])
    loop = _build_loop(tmp_path, provider, monkeypatch)
    loop.vision_model = "fake-vision-model"
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
    ]

    final, _ = asyncio.run(loop._run_execute_reflect_loop(messages, "trace-text"))
    assert final == "text final"
    assert provider.models[-1] == "fake-model"


def test_run_execute_reflect_loop_uses_vision_model_for_media_image_references(
    tmp_path: Path, monkeypatch
) -> None:
    provider = FakeProvider([LLMResponse(content="vision ref final")])
    loop = _build_loop(tmp_path, provider, monkeypatch)
    loop.vision_model = "fake-vision-model"
    messages = [
        {"role": "system", "content": "sys"},
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "Attached media references:\n- media://feishu/image/img_123",
                },
                {"type": "text", "text": "describe it"},
            ],
        },
    ]

    final, _ = asyncio.run(loop._run_execute_reflect_loop(messages, "trace-vision-ref"))
    assert final == "vision ref final"
    assert provider.models[-1] == "fake-vision-model"


def test_record_tool_learning_dedupes_same_signature(tmp_path: Path, monkeypatch) -> None:
    provider = FakeProvider([LLMResponse(content="ok")])
    loop = _build_loop(tmp_path, provider, monkeypatch)

    loop._record_tool_learning(
        tool_name="dummy_search",
        failed_args={"q": "a"},
        corrected_args={"query": "a"},
        error_message="missing query",
        trace_id="trace-1",
    )
    loop._record_tool_learning(
        tool_name="dummy_search",
        failed_args={"q": "a"},
        corrected_args={"query": "a"},
        error_message="missing query again",
        trace_id="trace-2",
    )

    learning_file = tmp_path / "memory" / "TOOLS_LEARNING.md"
    text = learning_file.read_text(encoding="utf-8")
    assert text.count("tool=dummy_search") == 1


def test_auto_parameter_rewrite_applies_learned_mapping_before_execution(
    tmp_path: Path, monkeypatch
) -> None:
    provider = FakeProvider(
        [
            LLMResponse(
                content="call with old args",
                tool_calls=[
                    ToolCallRequest(id="t1", name="dummy_search", arguments={"q": "openclaw"})
                ],
            ),
            LLMResponse(content="done with rewrite"),
        ]
    )
    loop = _build_loop(tmp_path, provider, monkeypatch)
    loop.auto_parameter_rewrite = True
    reg = ToolRegistry(policy=ToolPolicyEngine())
    reg.register(DummySearchTool())
    loop.tools = reg

    learning_file = tmp_path / "memory" / "TOOLS_LEARNING.md"
    learning_file.parent.mkdir(parents=True, exist_ok=True)
    learning_file.write_text(
        "# Tool Learning\n\n"
        '- 2026-02-17T10:01:00Z tool=dummy_search sig=b2 error="missing query" from={"q":"openclaw"} to={"query":"openclaw"} trace_id=t2\n',
        encoding="utf-8",
    )

    msgs = [{"role": "user", "content": "search openclaw"}]
    final, out = asyncio.run(loop._run_execute_reflect_loop(msgs, "trace-rewrite"))
    assert final == "done with rewrite"
    assert any(
        m.get("role") == "tool" and "result for openclaw" in str(m.get("content")) for m in out
    )

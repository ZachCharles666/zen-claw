import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zen_claw.agent.loop import AgentLoop
from zen_claw.bus.queue import MessageBus
from zen_claw.providers.base import LLMProvider, LLMResponse


class _FakeProvider(LLMProvider):
    def __init__(self):
        super().__init__(api_key=None, api_base=None)
        self.snapshots: list[list[dict[str, Any]]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        self.snapshots.append(messages)
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "fake-model"


def test_agent_loop_passes_memory_recall_mode_to_context(tmp_path: Path, monkeypatch) -> None:
    @dataclass
    class _Session:
        key: str
        messages: list[dict[str, Any]] = field(default_factory=list)
        metadata: dict[str, Any] = field(default_factory=dict)

        def add_message(self, role: str, content: str, **kwargs: Any) -> None:
            self.messages.append({"role": role, "content": content, **kwargs})

        def get_history(self, max_messages: int = 50) -> list[dict[str, Any]]:
            recent = self.messages[-max_messages:] if len(self.messages) > max_messages else self.messages
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

    monkeypatch.setattr("zen_claw.agent.loop.SessionManager", _InMemorySessionManager)
    provider = _FakeProvider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        memory_recall_mode="none",
        enable_planning=False,
        max_reflections=2,
    )
    assert loop.memory_recall_mode == "none"
    assert loop.context.memory_recall_mode == "none"
    assert loop.execution.enable_planning is False
    assert loop.execution.max_reflections == 2

    # Ensure recent mode is passed through and injected at loop runtime.
    loop_recent = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        memory_recall_mode="recent",
        enable_planning=False,
        max_reflections=1,
    )
    loop_recent.memory_extractor.should_extract = lambda user_text, assistant_text: False  # type: ignore[assignment]
    loop_recent.context.memory.append_today("- recent memory from loop test")
    _ = asyncio.run(loop_recent.process_direct("check recent memory", channel="cli", chat_id="direct"))
    system_prompt = str(provider.snapshots[-1][0]["content"])
    assert "## Recent Memory" in system_prompt
    assert "recent memory from loop test" in system_prompt



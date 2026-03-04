import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zen_claw.agent.loop import AgentLoop
from zen_claw.bus.queue import MessageBus
from zen_claw.providers.base import LLMProvider, LLMResponse


class _QueueProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]):
        super().__init__(api_key=None, api_base=None)
        self._responses = list(responses)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        if self._responses:
            return self._responses.pop(0)
        return LLMResponse(content='{"should_write": false, "memory_type": "daily", "content": ""}')

    def get_default_model(self) -> str:
        return "fake-model"


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


def test_memory_write_policy_long_term_deduplicates(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("zen_claw.agent.loop.SessionManager", _InMemorySessionManager)
    provider = _QueueProvider(
        [
            LLMResponse(
                content='{"should_write": true, "memory_type": "long_term", "content": "User prefers concise progress reports."}'
            ),
            LLMResponse(
                content='{"should_write": true, "memory_type": "long_term", "content": "User prefers concise progress reports."}'
            ),
        ]
    )
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        enable_planning=False,
        max_reflections=1,
    )
    loop.memory_extractor.should_extract = lambda user_text, assistant_text: True  # type: ignore[assignment]

    asyncio.run(loop._extract_and_store_memory("user says x", "assistant says y", "trace-1"))
    asyncio.run(loop._extract_and_store_memory("user says x2", "assistant says y2", "trace-2"))

    long_term = loop.context.memory.read_long_term()
    assert long_term.count("User prefers concise progress reports.") == 1


def test_memory_write_policy_daily_routes_to_today_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("zen_claw.agent.loop.SessionManager", _InMemorySessionManager)
    provider = _QueueProvider(
        [
            LLMResponse(
                content='{"should_write": true, "memory_type": "daily", "content": "Current sprint focuses on policy hardening."}'
            )
        ]
    )
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        enable_planning=False,
        max_reflections=1,
    )
    loop.memory_extractor.should_extract = lambda user_text, assistant_text: True  # type: ignore[assignment]

    asyncio.run(loop._extract_and_store_memory("u", "a", "trace-3"))

    today = loop.context.memory.read_today()
    assert "Current sprint focuses on policy hardening." in today
    assert "## Long-term Memory" not in today


def test_memory_write_policy_skips_when_should_write_false(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("zen_claw.agent.loop.SessionManager", _InMemorySessionManager)
    provider = _QueueProvider(
        [LLMResponse(content='{"should_write": false, "memory_type": "long_term", "content": "Ignore me."}')]
    )
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        enable_planning=False,
        max_reflections=1,
    )
    loop.memory_extractor.should_extract = lambda user_text, assistant_text: True  # type: ignore[assignment]

    asyncio.run(loop._extract_and_store_memory("u", "a", "trace-4"))

    assert loop.context.memory.read_long_term() == ""
    assert loop.context.memory.read_today() == ""



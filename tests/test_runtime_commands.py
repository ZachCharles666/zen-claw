import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import zen_claw.agent.loop as _loop_module
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

    def clear(self) -> None:
        self.messages = []


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


class _Provider(LLMProvider):
    def __init__(self):
        super().__init__(api_key=None, api_base=None)
        self.called_models: list[str] = []

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.called_models.append(str(model or ""))
        return LLMResponse(
            content="ok",
            tool_calls=[],
            usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        )

    def get_default_model(self) -> str:
        return "default-model"


def _make_loop(tmp_path: Path) -> tuple[AgentLoop, _Provider]:
    provider = _Provider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="default-model",
        allowed_models=["default-model", "model-a"],
        enable_planning=False,
        max_iterations=2,
    )
    loop.sessions = _InMemorySessionManager(tmp_path)
    return loop, provider


def test_runtime_model_switch_is_session_local(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(_loop_module, "SessionManager", _InMemorySessionManager)
    loop, provider = _make_loop(tmp_path)

    out1 = asyncio.run(loop.process_direct("/model model-a", session_key="cli:s1"))
    assert "model-a" in out1

    asyncio.run(loop.process_direct("hello s1", session_key="cli:s1"))
    asyncio.run(loop.process_direct("hello s2", session_key="cli:s2"))
    assert provider.called_models[0] == "model-a"
    assert provider.called_models[1] == "default-model"


def test_runtime_usage_verbose_and_clear(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(_loop_module, "SessionManager", _InMemorySessionManager)
    loop, _provider = _make_loop(tmp_path)

    out_verbose = asyncio.run(loop.process_direct("/verbose on", session_key="cli:s1"))
    assert "on" in out_verbose

    out_msg = asyncio.run(loop.process_direct("run", session_key="cli:s1"))
    assert "[verbose]" in out_msg

    out_usage = asyncio.run(loop.process_direct("/usage", session_key="cli:s1"))
    assert "total_tokens=12" in out_usage

    out_clear = asyncio.run(loop.process_direct("/clear", session_key="cli:s1"))
    assert "已清空" in out_clear
    session = loop.sessions.get_or_create("cli:s1")
    assert session.messages == []

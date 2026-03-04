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


def test_process_direct_accepts_media_refs_and_injects_into_user_content(tmp_path: Path, monkeypatch) -> None:
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
    monkeypatch.setattr("zen_claw.agent.skills.SkillsLoader.get_always_skills", lambda self: [])
    monkeypatch.setattr("zen_claw.agent.skills.SkillsLoader.build_skills_summary", lambda self: "")
    monkeypatch.setattr("zen_claw.agent.skills.SkillsLoader.load_skills_for_context", lambda self, names: "")

    provider = _FakeProvider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        memory_recall_mode="none",
        enable_planning=False,
        max_reflections=1,
    )
    loop.memory_extractor.should_extract = lambda user_text, assistant_text: False  # type: ignore[assignment]

    _ = asyncio.run(
        loop.process_direct(
            "handle attachment",
            channel="cli",
            chat_id="direct",
            media=["feishu://image/img_key_123"],
        )
    )
    msgs = provider.snapshots[-1]
    assert msgs[-1]["role"] == "user"
    content = msgs[-1]["content"]
    assert isinstance(content, list)
    assert any(
        part.get("type") == "text"
        and "Attached media references:" in str(part.get("text"))
        and "feishu://image/img_key_123" in str(part.get("text"))
        for part in content
    )




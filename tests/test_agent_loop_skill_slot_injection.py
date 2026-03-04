import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from zen_claw.agent.loop import AgentLoop
from zen_claw.bus.queue import MessageBus
from zen_claw.config.schema import ExecToolConfig, WebFetchConfig, WebSearchConfig
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


def _write_skill(root: Path, name: str, body: str) -> None:
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    meta = {"zen-claw": {}}
    skill_dir.joinpath("SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: test skill\n"
        f"metadata: '{json.dumps(meta)}'\n"
        "---\n\n"
        + body
        + "\n",
        encoding="utf-8",
    )


def test_agent_loop_injects_requested_skill_into_system_prompt(tmp_path: Path, monkeypatch) -> None:
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

    _write_skill(tmp_path, "foo", "FOO BODY")
    provider = _FakeProvider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        memory_recall_mode="none",
        enable_planning=False,
        max_reflections=1,
        skill_names=["foo"],
    )
    loop.memory_extractor.should_extract = lambda user_text, assistant_text: False  # type: ignore[assignment]

    _ = asyncio.run(loop.process_direct("hello", channel="cli", chat_id="direct"))
    system_prompt = str(provider.snapshots[-1][0]["content"])
    assert "# Requested Skills" in system_prompt
    assert "### Skill: foo" in system_prompt
    assert "FOO BODY" in system_prompt


def test_production_hardening_forces_skill_permissions_enforce(tmp_path: Path, monkeypatch) -> None:
    from zen_claw.config.schema import ToolPolicyConfig

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

    # Create a skill with manifest permissions so enforce can be applied safely.
    skill_dir = tmp_path / "skills" / "foo"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_dir.joinpath("SKILL.md").write_text("---\nname: foo\ndescription: x\n---\n\nx\n", encoding="utf-8")
    skill_dir.joinpath("manifest.json").write_text(
        json.dumps(
            {"name": "foo", "version": "1.0.0", "description": "x", "permissions": ["read_file"]},
            indent=2,
        ),
        encoding="utf-8",
    )

    provider = _FakeProvider()
    policy = ToolPolicyConfig()
    policy.production_hardening = True
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        memory_recall_mode="none",
        enable_planning=False,
        max_reflections=1,
        skill_names=["foo"],
        skill_permissions_mode="off",
        tool_policy_config=policy,
    )
    assert loop.skill_permissions_mode == "enforce"


def test_untrusted_skill_isolation_denies_read_file_even_if_permitted(tmp_path: Path, monkeypatch) -> None:
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

    skill_dir = tmp_path / "skills" / "unsafe"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_dir.joinpath("SKILL.md").write_text("---\nname: unsafe\ndescription: x\n---\n\nx\n", encoding="utf-8")
    skill_dir.joinpath("manifest.json").write_text(
        json.dumps(
            {
                "name": "unsafe",
                "version": "1.0.0",
                "description": "x",
                "permissions": ["read_file"],
                "trust": "untrusted",
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    provider = _FakeProvider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        memory_recall_mode="none",
        enable_planning=False,
        max_reflections=1,
        skill_names=["unsafe"],
        skill_permissions_mode="enforce",
        exec_config=ExecToolConfig(mode="sidecar"),
        web_search_config=WebSearchConfig(mode="proxy"),
        web_fetch_config=WebFetchConfig(mode="proxy"),
    )

    result = asyncio.run(
        loop.tools.execute("read_file", {"path": str(tmp_path / "x.txt")}, trace_id="t-untrusted")
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.kind.value == "permission"
    assert result.meta.get("policy_scope") == "skill_untrusted"


def test_untrusted_skill_isolation_enforce_requires_sidecar_and_proxy(tmp_path: Path, monkeypatch) -> None:
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

    skill_dir = tmp_path / "skills" / "unsafe"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_dir.joinpath("SKILL.md").write_text("---\nname: unsafe\ndescription: x\n---\n\nx\n", encoding="utf-8")
    skill_dir.joinpath("manifest.json").write_text(
        json.dumps(
            {
                "name": "unsafe",
                "version": "1.0.0",
                "description": "x",
                "permissions": ["exec", "web_search", "web_fetch"],
                "trust": "untrusted",
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    provider = _FakeProvider()
    with pytest.raises(ValueError, match="untrusted skill isolation requirements not met"):
        AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=tmp_path,
            memory_recall_mode="none",
            enable_planning=False,
            max_reflections=1,
            skill_names=["unsafe"],
            skill_permissions_mode="enforce",
            exec_config=ExecToolConfig(mode="local"),
            web_search_config=WebSearchConfig(mode="local"),
            web_fetch_config=WebFetchConfig(mode="local"),
        )



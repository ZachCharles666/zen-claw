import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from zen_claw.cli.commands import app
from zen_claw.config.schema import Config
from zen_claw.providers.base import LLMProvider, LLMResponse


class _FakeProvider(LLMProvider):
    def __init__(self):
        super().__init__(api_key=None, api_base=None)
        self.snapshots = []

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.snapshots.append(messages)
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "fake-model"


def test_agent_cli_media_option_injects_refs_into_user_content(tmp_path: Path, monkeypatch) -> None:
    @dataclass
    class _Session:
        key: str
        messages: list[dict[str, Any]] = field(default_factory=list)
        metadata: dict[str, Any] = field(default_factory=dict)

        def add_message(self, role: str, content: str, **kwargs: Any) -> None:
            self.messages.append({"role": role, "content": content, **kwargs})

        def get_history(self, max_messages: int = 50) -> list[dict[str, Any]]:
            recent = (
                self.messages[-max_messages:]
                if len(self.messages) > max_messages
                else self.messages
            )
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

    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path)
    cfg.agents.defaults.enable_planning = False

    provider = _FakeProvider()

    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda: cfg)
    monkeypatch.setattr("zen_claw.cli.commands._make_provider", lambda _cfg: provider)
    monkeypatch.setattr("zen_claw.cli.commands._print_effective_tool_backends", lambda _cfg: None)
    monkeypatch.setattr("zen_claw.agent.loop.SessionManager", _InMemorySessionManager)
    monkeypatch.setattr("zen_claw.agent.skills.SkillsLoader.get_always_skills", lambda self: [])
    monkeypatch.setattr("zen_claw.agent.skills.SkillsLoader.build_skills_summary", lambda self: "")
    monkeypatch.setattr(
        "zen_claw.agent.skills.SkillsLoader.load_skills_for_context", lambda self, names: ""
    )

    out = CliRunner().invoke(
        app,
        ["agent", "-m", "handle attachment", "--media", "feishu://image/img_key_123"],
    )
    assert out.exit_code == 0

    # Inspect the last LLM call input to ensure the user content contains the ref block.
    messages = provider.snapshots[-1]
    user = next(m for m in reversed(messages) if m.get("role") == "user")
    content = user["content"]
    assert isinstance(content, list)
    serialized = json.dumps(content, ensure_ascii=False)
    assert "Attached media references" in serialized
    assert "feishu://image/img_key_123" in serialized

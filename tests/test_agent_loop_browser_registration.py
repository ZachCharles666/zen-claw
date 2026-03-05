from pathlib import Path
from typing import Any

from zen_claw.agent.loop import AgentLoop
from zen_claw.bus.queue import MessageBus
from zen_claw.config.schema import BrowserToolConfig
from zen_claw.providers.base import LLMProvider, LLMResponse


class _FakeProvider(LLMProvider):
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "fake-model"


class _InMemorySessionManager:
    def __init__(self, workspace: Path):
        self.workspace = workspace

    def get_or_create(self, key: str):
        class _Session:
            def __init__(self, session_key: str):
                self.key = session_key
                self.messages: list[dict[str, Any]] = []
                self.metadata: dict[str, Any] = {}

            def add_message(self, role: str, content: str, **kwargs: Any) -> None:
                self.messages.append({"role": role, "content": content, **kwargs})

            def get_history(self, max_messages: int = 50) -> list[dict[str, Any]]:
                recent = (
                    self.messages[-max_messages:]
                    if len(self.messages) > max_messages
                    else self.messages
                )
                return [{"role": m["role"], "content": m["content"]} for m in recent]

        return _Session(key)

    def save(self, session) -> None:
        return None


def test_agent_loop_registers_browser_tools_when_sidecar_enabled(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("zen_claw.agent.loop.SessionManager", _InMemorySessionManager)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_FakeProvider(),
        workspace=tmp_path,
        browser_config=BrowserToolConfig(mode="sidecar"),
    )
    names = set(loop.tools.tool_names)
    assert "browser_open" in names
    assert "browser_click" in names
    assert "browser_type" in names
    assert "browser_extract" in names
    assert "browser_screenshot" in names
    assert "browser_save_session" in names
    assert "browser_load_session" in names


def test_agent_loop_does_not_register_browser_tools_when_off(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("zen_claw.agent.loop.SessionManager", _InMemorySessionManager)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_FakeProvider(),
        workspace=tmp_path,
        browser_config=BrowserToolConfig(mode="off"),
    )
    names = set(loop.tools.tool_names)
    assert "browser_open" not in names
    assert "browser_click" not in names
    assert "browser_type" not in names
    assert "browser_extract" not in names
    assert "browser_screenshot" not in names
    assert "browser_save_session" not in names
    assert "browser_load_session" not in names

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zen_claw.agent.loop import AgentLoop
from zen_claw.bus.events import InboundMessage
from zen_claw.bus.queue import MessageBus
from zen_claw.config.schema import ToolPolicyConfig, ToolPolicyLayerConfig
from zen_claw.cron.service import CronService
from zen_claw.providers.base import LLMProvider, LLMResponse, ToolCallRequest


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


class _QueueProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]):
        super().__init__(api_key=None, api_base=None)
        self._responses = list(responses)
        self.calls = 0
        self.snapshots: list[list[dict[str, Any]]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        self.calls += 1
        self.snapshots.append(messages)
        if self._responses:
            return self._responses.pop(0)
        return LLMResponse(content="done")

    def get_default_model(self) -> str:
        return "fake-model"


def test_agent_loop_plan_execute_end_to_end(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("zen_claw.agent.loop.SessionManager", _InMemorySessionManager)
    provider = _QueueProvider(
        [
            LLMResponse(content="1) list dir"),
            LLMResponse(
                content="call list_dir",
                tool_calls=[ToolCallRequest(id="t1", name="list_dir", arguments={"path": "."})],
            ),
            LLMResponse(content="done"),
        ]
    )
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="fake-model",
        max_iterations=4,
        brave_api_key=None,
        restrict_to_workspace=True,
        enable_planning=True,
        max_reflections=1,
    )

    out = asyncio.run(loop.process_direct("list files", channel="cli", chat_id="direct"))
    assert out == "done"
    assert provider.calls == 3
    # Second provider call is execute loop input and should include injected plan.
    assert any("[Plan]" in str(m.get("content")) for m in provider.snapshots[1] if m.get("role") == "assistant")
    saved = loop.sessions.get_or_create("cli:direct")
    assert [m["role"] for m in saved.messages] == ["user", "assistant"]


def test_agent_loop_reflection_retries_after_parameter_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("zen_claw.agent.loop.SessionManager", _InMemorySessionManager)
    provider = _QueueProvider(
        [
            LLMResponse(
                content="bad tool args",
                tool_calls=[ToolCallRequest(id="t1", name="list_dir", arguments={})],
            ),
            LLMResponse(
                content="fixed args",
                tool_calls=[ToolCallRequest(id="t2", name="list_dir", arguments={"path": "."})],
            ),
            LLMResponse(content="recovered"),
        ]
    )
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="fake-model",
        max_iterations=4,
        brave_api_key=None,
        restrict_to_workspace=True,
        enable_planning=False,
        max_reflections=1,
    )

    out = asyncio.run(loop.process_direct("list files", channel="cli", chat_id="direct"))
    assert out == "recovered"
    assert provider.calls == 3
    # Second execute call should include reflection guidance after first parameter failure.
    assert any(
        "Previous tool attempts failed" in str(m.get("content"))
        for m in provider.snapshots[1]
        if m.get("role") == "user"
    )


def test_agent_loop_applies_channel_policy_deny(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("zen_claw.agent.loop.SessionManager", _InMemorySessionManager)
    provider = _QueueProvider(
        [
            LLMResponse(
                content="call disallowed tool",
                tool_calls=[ToolCallRequest(id="t1", name="list_dir", arguments={"path": "."})],
            ),
            LLMResponse(content="fallback"),
        ]
    )
    policy = ToolPolicyConfig(
        channel_policies={
            "discord": ToolPolicyLayerConfig(deny=["list_dir"]),
        }
    )
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="fake-model",
        max_iterations=3,
        brave_api_key=None,
        restrict_to_workspace=True,
        enable_planning=False,
        max_reflections=1,
        tool_policy_config=policy,
    )

    out = asyncio.run(loop.process_direct("list files", channel="discord", chat_id="direct"))
    assert out == "fallback"
    assert provider.calls == 2
    assert any(
        "kind=permission" in str(m.get("content"))
        for m in provider.snapshots[1]
        if m.get("role") == "user"
    )


def test_agent_loop_channel_policy_scope_is_cleared_between_channels(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("zen_claw.agent.loop.SessionManager", _InMemorySessionManager)
    provider = _QueueProvider(
        [
            LLMResponse(
                content="discord denied attempt",
                tool_calls=[ToolCallRequest(id="d1", name="list_dir", arguments={"path": "."})],
            ),
            LLMResponse(content="discord fallback"),
            LLMResponse(
                content="cli allowed attempt",
                tool_calls=[ToolCallRequest(id="c1", name="list_dir", arguments={"path": "."})],
            ),
            LLMResponse(content="cli success"),
        ]
    )
    policy = ToolPolicyConfig(
        channel_policies={
            "discord": ToolPolicyLayerConfig(deny=["list_dir"]),
        }
    )
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="fake-model",
        max_iterations=3,
        brave_api_key=None,
        restrict_to_workspace=True,
        enable_planning=False,
        max_reflections=1,
        tool_policy_config=policy,
    )

    out_discord = asyncio.run(loop.process_direct("list files", channel="discord", chat_id="direct"))
    out_cli = asyncio.run(loop.process_direct("list files", channel="cli", chat_id="direct"))
    assert out_discord == "discord fallback"
    assert out_cli == "cli success"


def test_agent_loop_cron_allowed_channels_blocks_disallowed_channel(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("zen_claw.agent.loop.SessionManager", _InMemorySessionManager)
    provider = _QueueProvider(
        [
            LLMResponse(
                content="try cron",
                tool_calls=[
                    ToolCallRequest(
                        id="c1",
                        name="cron",
                        arguments={"action": "add", "message": "ping", "every_seconds": 60},
                    )
                ],
            ),
            LLMResponse(content="cron denied fallback"),
        ]
    )
    policy = ToolPolicyConfig(cron_allowed_channels=["telegram"])
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="fake-model",
        max_iterations=3,
        brave_api_key=None,
        restrict_to_workspace=True,
        enable_planning=False,
        max_reflections=1,
        tool_policy_config=policy,
        cron_service=CronService(tmp_path / "cron_jobs.json"),
    )
    loop.memory_extractor.should_extract = lambda user_text, assistant_text: False  # type: ignore[assignment]

    out = asyncio.run(loop.process_direct("schedule reminder", channel="discord", chat_id="u1"))
    assert out == "cron denied fallback"
    assert provider.calls == 2
    assert any(
        "kind=permission" in str(m.get("content"))
        for m in provider.snapshots[1]
        if m.get("role") == "user"
    )


def test_agent_loop_system_message_applies_origin_channel_policy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("zen_claw.agent.loop.SessionManager", _InMemorySessionManager)
    provider = _QueueProvider(
        [
            LLMResponse(
                content="call list_dir via system route",
                tool_calls=[ToolCallRequest(id="t1", name="list_dir", arguments={"path": "."})],
            ),
            LLMResponse(content="policy fallback"),
        ]
    )
    policy = ToolPolicyConfig(
        channel_policies={
            "discord": ToolPolicyLayerConfig(deny=["list_dir"]),
        }
    )
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="fake-model",
        max_iterations=3,
        brave_api_key=None,
        restrict_to_workspace=True,
        enable_planning=False,
        max_reflections=1,
        tool_policy_config=policy,
    )
    loop.memory_extractor.should_extract = lambda user_text, assistant_text: False  # type: ignore[assignment]

    system_msg = InboundMessage(
        channel="system",
        sender_id="subagent-1",
        chat_id="discord:u1",
        content="continue from background task",
    )
    out = asyncio.run(loop._process_message(system_msg))
    assert out is not None
    assert out.channel == "discord"
    assert out.chat_id == "u1"
    assert out.content == "policy fallback"
    assert provider.calls == 2


def test_agent_loop_multimodal_media_refs_are_injected_and_filtered(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("zen_claw.agent.loop.SessionManager", _InMemorySessionManager)
    provider = _QueueProvider([LLMResponse(content="media handled")])
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="fake-model",
        max_iterations=2,
        brave_api_key=None,
        restrict_to_workspace=True,
        enable_planning=False,
        max_reflections=1,
    )
    loop.memory_extractor.should_extract = lambda user_text, assistant_text: False  # type: ignore[assignment]

    inbound = InboundMessage(
        channel="whatsapp",
        sender_id="u1",
        chat_id="u1",
        content="analyze attached media",
        media=["whatsapp://image/img_1", "https://example.com/blocked.mp4"],
    )
    out = asyncio.run(loop._process_message(inbound))
    assert out is not None
    assert out.content == "media handled"
    assert provider.calls == 1

    # Last user message payload passed to provider should include allowed refs only.
    user_msg = provider.snapshots[0][-1]
    assert user_msg["role"] == "user"
    payload = user_msg["content"]
    assert isinstance(payload, list)
    refs_text = "\n".join(
        str(p.get("text", ""))
        for p in payload
        if isinstance(p, dict) and p.get("type") == "text"
    )
    assert "whatsapp://image/img_1" in refs_text
    assert "https://example.com/blocked.mp4" not in refs_text



import json
from types import SimpleNamespace
from typing import Any

from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.registry import ToolRegistry
from zen_claw.bus.queue import MessageBus
from zen_claw.channels.base import BaseChannel
from zen_claw.observability.trace import TraceContext


class _DummyChannel(BaseChannel):
    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg: Any) -> None:
        return None


class _EchoTool(Tool):
    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "echo"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        }

    async def execute(self, **kwargs: Any) -> str:
        return str(kwargs["value"])


def test_trace_context_preserves_existing_trace_id() -> None:
    trace_id, metadata = TraceContext.ensure_trace_id({"trace_id": "abc123"})
    assert trace_id == "abc123"
    assert metadata["trace_id"] == "abc123"


def test_trace_context_creates_trace_id_when_missing() -> None:
    trace_id, metadata = TraceContext.ensure_trace_id({})
    assert trace_id
    assert metadata["trace_id"] == trace_id


def test_trace_context_event_text_has_required_schema_keys() -> None:
    payload = TraceContext.event_text("agent.inbound", "t-1")
    parsed = json.loads(payload)
    assert parsed["event"] == "agent.inbound"
    assert parsed["trace_id"] == "t-1"
    assert "error_kind" in parsed
    assert "retryable" in parsed


async def test_channel_handle_message_injects_trace_id() -> None:
    bus = MessageBus()
    channel = _DummyChannel(config=SimpleNamespace(admins=["u1"], users=[], allow_from=[]), bus=bus)

    await channel._handle_message(
        sender_id="u1",
        chat_id="c1",
        content="hello",
        metadata={},
    )
    msg = await bus.consume_inbound()
    assert msg.trace_id
    assert msg.metadata["trace_id"] == msg.trace_id


async def test_registry_execute_accepts_trace_id() -> None:
    registry = ToolRegistry()
    registry.register(_EchoTool())

    result = await registry.execute("echo", {"value": "ok"}, trace_id="t-1")
    assert result.ok is True
    assert result.content == "ok"



import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pytest

from zen_claw.agent.router import AgentRouter
from zen_claw.bus.events import InboundMessage
from zen_claw.bus.queue import MessageBus
from zen_claw.cli.commands import _process_inbound_with_agent_router
from zen_claw.config.loader import convert_keys
from zen_claw.config.schema import Config


@dataclass
class _FakeLoop:
    name: str
    seen: list[dict[str, Any]] = field(default_factory=list)

    async def _process_message(self, msg: InboundMessage):
        self.seen.append(
            {
                "content": msg.content,
                "sender_id": msg.sender_id,
                "chat_id": msg.chat_id,
                "metadata": dict(msg.metadata),
            }
        )
        from zen_claw.bus.events import OutboundMessage

        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=f"handled:{self.name}")


class _FakePool:
    def __init__(self):
        self.loops: dict[str, _FakeLoop] = {}

    async def get_or_create(self, agent_id: str):
        if agent_id not in self.loops:
            self.loops[agent_id] = _FakeLoop(agent_id)
        return self.loops[agent_id]


class _FakeChannelManager:
    def __init__(self):
        self.bound: list[dict[str, str]] = []

    def bind_agent(self, *, channel: str, chat_id: str, user_id: str, agent_id: str, reason: str):
        self.bound.append(
            {
                "channel": channel,
                "chat_id": chat_id,
                "user_id": user_id,
                "agent_id": agent_id,
                "reason": reason,
            }
        )
        return agent_id


@pytest.mark.asyncio
async def test_process_inbound_with_agent_router_routes_and_persists_auto_binding() -> None:
    cfg = Config.model_validate(
        convert_keys(
            {
                "agents": {
                    "profiles": {
                        "finance_writer": {"routingKeywords": ["finance report"]},
                    }
                }
            }
        )
    )
    router = AgentRouter(cfg)
    bus = MessageBus()
    default_loop = _FakeLoop("default")
    pool = _FakePool()
    channels = _FakeChannelManager()
    msg = InboundMessage(
        channel="webchat",
        sender_id="user-1",
        chat_id="chat-1",
        content="please draft a finance report",
        timestamp=datetime.now(),
        metadata={},
    )

    await _process_inbound_with_agent_router(
        msg,
        router=router,
        default_agent=default_loop,
        agent_pool=pool,
        bus=bus,
        channel_manager=channels,
    )

    outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=0.1)
    assert outbound.content == "handled:finance_writer"
    assert default_loop.seen == []
    assert pool.loops["finance_writer"].seen[0]["metadata"]["routed_agent_id"] == "finance_writer"
    assert pool.loops["finance_writer"].seen[0]["metadata"]["routed_agent_reason"] == "keyword"
    assert channels.bound == [
        {
            "channel": "webchat",
            "chat_id": "chat-1",
            "user_id": "user-1",
            "agent_id": "finance_writer",
            "reason": "auto_route:keyword",
        }
    ]


@pytest.mark.asyncio
async def test_process_inbound_with_agent_router_keeps_default_loop_for_default_decision() -> None:
    cfg = Config()
    router = AgentRouter(cfg)
    bus = MessageBus()
    default_loop = _FakeLoop("default")
    pool = _FakePool()
    msg = InboundMessage(
        channel="cli",
        sender_id="user-1",
        chat_id="chat-1",
        content="hello",
        timestamp=datetime.now(),
        metadata={},
    )

    await _process_inbound_with_agent_router(
        msg,
        router=router,
        default_agent=default_loop,
        agent_pool=pool,
        bus=bus,
        channel_manager=None,
    )

    outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=0.1)
    assert outbound.content == "handled:default"
    assert len(default_loop.seen) == 1
    assert pool.loops == {}

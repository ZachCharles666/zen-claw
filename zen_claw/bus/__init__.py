"""Message bus module for decoupled channel-agent communication."""

from zen_claw.bus.events import InboundMessage, OutboundMessage
from zen_claw.bus.queue import MessageBus

__all__ = ["MessageBus", "InboundMessage", "OutboundMessage"]

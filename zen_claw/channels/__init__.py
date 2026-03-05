"""Chat channels module with plugin architecture."""

from zen_claw.channels.base import BaseChannel
from zen_claw.channels.manager import ChannelManager

__all__ = ["BaseChannel", "ChannelManager"]

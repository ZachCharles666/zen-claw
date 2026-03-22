"""Chat channels module with plugin architecture."""

from zen_claw.channels.base import BaseChannel
from zen_claw.channels.manager import ChannelManager
from zen_claw.channels.registry import CHANNEL_SPECS, ChannelSpec, iter_channel_specs

__all__ = ["BaseChannel", "ChannelManager", "ChannelSpec", "CHANNEL_SPECS", "iter_channel_specs"]

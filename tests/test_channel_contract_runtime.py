import importlib

import pytest

from zen_claw.channels.base import BaseChannel
from zen_claw.channels.registry import iter_channel_specs
from zen_claw.config.schema import Config


class _Bus:
    def __init__(self) -> None:
        self.messages = []

    async def publish_inbound(self, msg) -> None:
        self.messages.append(msg)


@pytest.mark.asyncio
async def test_registered_channels_share_base_contract_runtime_behavior() -> None:
    cfg = Config()
    checked = {"slack", "telegram", "webchat"}

    for spec in iter_channel_specs():
        if spec.key not in checked:
            continue
        channel_cfg = getattr(cfg.channels, spec.config_attr)
        channel_cfg.admins = ["admin-user"]
        channel_cfg.users = ["normal-user"]
        bus = _Bus()

        module = importlib.import_module(spec.module_path)
        channel_cls = getattr(module, spec.class_name)
        channel = channel_cls(channel_cfg, bus)

        assert isinstance(channel, BaseChannel)
        assert channel.is_running is False
        assert channel.get_role("admin-user") == "admin"
        assert channel.get_role("normal-user") == "user"
        assert channel.is_allowed("normal-user") is True
        assert channel.is_allowed("guest-user") is False

        await channel._handle_message(
            sender_id="normal-user",
            chat_id="chat-1",
            content="hello",
            media=[" media://one ", "media://one", ""],
            metadata={"media_refs": ["media://two"]},
        )

        assert len(bus.messages) == 1
        inbound = bus.messages[0]
        assert inbound.channel == spec.key
        assert inbound.chat_id == "chat-1"
        assert inbound.media == ["media://one", "media://two"]
        assert inbound.metadata["channel_role"] == "user"
        assert inbound.metadata["identity_verified"] is True

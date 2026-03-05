import sys
from pathlib import Path
from types import ModuleType

from zen_claw.bus.events import OutboundMessage
from zen_claw.bus.queue import MessageBus
from zen_claw.channels.manager import ChannelManager
from zen_claw.config.schema import Config


class _FakeTelegramChannel:
    def __init__(self, config, bus, groq_api_key="", media_root=None):
        self.config = config
        self.bus = bus
        self.groq_api_key = groq_api_key
        self.media_root = media_root
        self._running = False

    async def start(self):
        self._running = True

    async def stop(self):
        self._running = False

    async def send(self, msg):
        return None

    @property
    def is_running(self):
        return self._running


class _FakeDiscordChannel:
    def __init__(self, config, bus, media_root=None, groq_api_key=""):
        self.config = config
        self.bus = bus
        self.groq_api_key = groq_api_key
        self.media_root = media_root
        self._running = False

    async def start(self):
        self._running = True

    async def stop(self):
        self._running = False

    async def send(self, msg):
        return None

    @property
    def is_running(self):
        return self._running


class _FakeWhatsAppChannel:
    def __init__(self, config, bus, media_root=None):
        self.config = config
        self.bus = bus
        self.media_root = media_root
        self._running = False

    async def start(self):
        self._running = True

    async def stop(self):
        self._running = False

    async def send(self, msg):
        return None

    @property
    def is_running(self):
        return self._running


class _FakeFeishuChannel:
    def __init__(self, config, bus, media_root=None):
        self.config = config
        self.bus = bus
        self.media_root = media_root
        self._running = False

    async def start(self):
        self._running = True

    async def stop(self):
        self._running = False

    async def send(self, msg):
        return None

    @property
    def is_running(self):
        return self._running


def test_channel_manager_injects_workspace_media_root(monkeypatch, tmp_path: Path) -> None:
    tg_mod = ModuleType("zen_claw.channels.telegram")
    tg_mod.TelegramChannel = _FakeTelegramChannel
    dc_mod = ModuleType("zen_claw.channels.discord")
    dc_mod.DiscordChannel = _FakeDiscordChannel
    wa_mod = ModuleType("zen_claw.channels.whatsapp")
    wa_mod.WhatsAppChannel = _FakeWhatsAppChannel
    fs_mod = ModuleType("zen_claw.channels.feishu")
    fs_mod.FeishuChannel = _FakeFeishuChannel
    monkeypatch.setitem(sys.modules, "zen_claw.channels.telegram", tg_mod)
    monkeypatch.setitem(sys.modules, "zen_claw.channels.discord", dc_mod)
    monkeypatch.setitem(sys.modules, "zen_claw.channels.whatsapp", wa_mod)
    monkeypatch.setitem(sys.modules, "zen_claw.channels.feishu", fs_mod)

    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path / "ws")
    cfg.channels.telegram.enabled = True
    cfg.channels.discord.enabled = True
    cfg.channels.whatsapp.enabled = True
    cfg.channels.feishu.enabled = True
    cfg.providers.groq.api_key = "groq-test-key"

    mgr = ChannelManager(cfg, MessageBus())
    expected_media_root = cfg.workspace_path / "media"

    tg = mgr.channels["telegram"]
    dc = mgr.channels["discord"]
    wa = mgr.channels["whatsapp"]
    fs = mgr.channels["feishu"]
    assert tg.media_root == expected_media_root
    assert dc.media_root == expected_media_root
    assert wa.media_root == expected_media_root
    assert fs.media_root == expected_media_root
    assert tg.groq_api_key == "groq-test-key"
    assert dc.groq_api_key == "groq-test-key"
    assert callable(tg.access_checker)
    assert callable(dc.access_checker)
    assert callable(wa.access_checker)
    assert callable(fs.access_checker)


def test_channel_manager_global_allow_deny_rbac(tmp_path: Path) -> None:
    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path / "ws")
    cfg.channels.allow_from = ["allowed-global"]
    cfg.channels.deny_from = ["blocked-global"]
    mgr = ChannelManager(cfg, MessageBus())

    ch_cfg = cfg.channels.telegram
    assert mgr._is_sender_allowed("blocked-global", ch_cfg) is False
    assert mgr._is_sender_allowed("someone-else", ch_cfg) is False
    assert mgr._is_sender_allowed("allowed-global", ch_cfg) is True


async def test_channel_manager_drop_notice_respects_cooldown(tmp_path: Path) -> None:
    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path / "ws")
    cfg.channels.outbound_rate_limit_drop_notice = True
    cfg.channels.outbound_rate_limit_drop_notice_cooldown_sec = 30
    cfg.channels.outbound_rate_limit_drop_notice_text = "busy"
    mgr = ChannelManager(cfg, MessageBus())

    sent: list[str] = []

    class _FakeChannel:
        async def send(self, msg):
            sent.append(msg.content)

    msg = OutboundMessage(channel="discord", chat_id="u1", content="payload")
    await mgr._maybe_send_drop_notice(_FakeChannel(), msg, "t1")
    await mgr._maybe_send_drop_notice(_FakeChannel(), msg, "t2")
    assert sent == ["busy"]

    # Simulate cooldown elapsed.
    mgr._last_drop_notice_at["discord:u1"] = mgr._last_drop_notice_at["discord:u1"] - 31
    await mgr._maybe_send_drop_notice(_FakeChannel(), msg, "t3")
    assert sent == ["busy", "busy"]

import asyncio
from pathlib import Path

from zen_claw.bus.events import OutboundMessage
from zen_claw.bus.queue import MessageBus
from zen_claw.channels.manager import ChannelManager
from zen_claw.channels.signal import SignalChannel
from zen_claw.config.schema import Config, SignalConfig


def test_signal_channel_start_send_stop() -> None:
    async def _run() -> None:
        ch = SignalChannel(SignalConfig(enabled=True), MessageBus())

        async def _fake_post(path: str, payload: dict):
            return {"ok": True}

        ch._signald_post = _fake_post  # type: ignore[method-assign]

        await ch.start()
        assert ch.is_running is True
        await ch.send(OutboundMessage(channel="signal", chat_id="+100", content="hello"))
        await ch.stop()
        assert ch.is_running is False

    asyncio.run(_run())


def test_signal_channel_registered_by_manager(tmp_path) -> None:
    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path / "ws")
    cfg.channels.signal.enabled = True
    mgr = ChannelManager(cfg, MessageBus())
    assert "signal" in mgr.enabled_channels


def test_signal_channel_signald_send_payload() -> None:
    async def _run() -> None:
        bus = MessageBus()
        cfg = SignalConfig(enabled=True, mode="signald", account="+18880001")
        ch = SignalChannel(cfg, bus)
        captured: dict[str, object] = {}

        async def _fake_post(path: str, payload: dict):
            captured["path"] = path
            captured["payload"] = payload
            return {"ok": True}

        ch._signald_post = _fake_post  # type: ignore[method-assign]
        await ch.send(
            OutboundMessage(
                channel="signal",
                chat_id="+18880002",
                content="hello",
                media=["C:/tmp/a.jpg"],
            )
        )
        assert captured["path"] == "/v1/send"
        payload = captured["payload"]
        assert isinstance(payload, dict)
        assert payload["account"] == "+18880001"
        assert payload["recipientAddress"]["number"] == "+18880002"
        assert payload["messageBody"] == "hello"
        assert payload["attachments"] == ["C:/tmp/a.jpg"]

    asyncio.run(_run())


def test_signal_channel_process_signald_payload_to_bus() -> None:
    async def _run() -> None:
        bus = MessageBus()
        cfg = SignalConfig(enabled=True, allow_from=["+19990001"])
        ch = SignalChannel(cfg, bus)

        payload = [
            {
                "envelope": {
                    "source": "+19990001",
                    "dataMessage": {
                        "message": "from signal",
                        "attachments": [
                            {"id": "att1", "contentType": "image/jpeg", "path": "C:/tmp/pic.jpg"}
                        ],
                    },
                }
            }
        ]
        await ch._process_signald_payload(payload)  # type: ignore[attr-defined]
        assert bus.inbound_size == 1
        inbound = await bus.consume_inbound()
        assert inbound.channel == "signal"
        assert inbound.sender_id == "+19990001"
        assert inbound.chat_id == "+19990001"
        assert inbound.content == "from signal"
        assert "pic.jpg" in inbound.media[0]
        assert inbound.media[1] == "media://signal/image/att1"
        assert inbound.metadata.get("media_refs") == ["media://signal/image/att1"]

    asyncio.run(_run())


def test_signal_channel_send_falls_back_to_jsonrpc() -> None:
    async def _run() -> None:
        bus = MessageBus()
        cfg = SignalConfig(enabled=True, mode="signald", account="+17770001")
        ch = SignalChannel(cfg, bus)
        calls: list[tuple[str, object]] = []

        async def _fake_post(path: str, payload: dict):
            calls.append(("post", path))
            raise RuntimeError("404")

        async def _fake_rpc(methods: list[str], params: dict):
            calls.append(("rpc_multi", tuple(methods)))
            return {"ok": True}

        ch._signald_post = _fake_post  # type: ignore[method-assign]
        ch._signald_rpc_multi = _fake_rpc  # type: ignore[method-assign]
        await ch.send(OutboundMessage(channel="signal", chat_id="+17770002", content="hi"))
        assert calls == [
            ("post", "/v1/send"),
            ("rpc_multi", ("send", "sendMessage", "send_message")),
        ]

    asyncio.run(_run())


def test_signal_channel_downloads_attachment_when_no_path() -> None:
    async def _run() -> None:
        bus = MessageBus()
        cfg = SignalConfig(enabled=True, allow_from=["+12220001"], attachment_download=True)
        ch = SignalChannel(cfg, bus)

        async def _fake_download(att: dict, idx: int):
            assert idx == 0
            assert att.get("id") == "aid1"
            return "C:/tmp/signal-aid1.jpg"

        ch._download_signal_attachment = _fake_download  # type: ignore[method-assign]
        payload = [
            {
                "envelope": {
                    "source": "+12220001",
                    "dataMessage": {
                        "message": "photo incoming",
                        "attachments": [{"id": "aid1", "contentType": "image/jpeg"}],
                    },
                }
            }
        ]
        await ch._process_signald_payload(payload)  # type: ignore[attr-defined]
        inbound = await bus.consume_inbound()
        assert inbound.content == "photo incoming"
        assert inbound.media[0] == "C:/tmp/signal-aid1.jpg"
        assert inbound.media[1] == "media://signal/image/aid1"

    asyncio.run(_run())


def test_signal_channel_attachment_rpc_fallback(tmp_path) -> None:
    async def _run() -> None:
        bus = MessageBus()
        cfg = SignalConfig(enabled=True, attachment_download=True)
        ch = SignalChannel(cfg, bus, media_root=tmp_path / "media")

        class _Resp:
            status_code = 404
            content = b""

            def raise_for_status(self):
                raise RuntimeError("404")

        class _Http:
            async def get(self, _url: str):
                return _Resp()

        async def _fake_rpc_multi(methods: list[str], _params: dict):
            assert methods[0] == "getAttachment"
            return {"base64": "aGVsbG8="}

        ch._http = _Http()  # type: ignore[assignment]
        ch._signald_rpc_multi = _fake_rpc_multi  # type: ignore[method-assign]
        result = await ch._download_signal_attachment(
            {"id": "aid-rpc", "contentType": "image/jpeg"}, 0
        )
        assert result is not None
        assert Path(result).exists()
        Path(result).unlink(missing_ok=True)

    asyncio.run(_run())

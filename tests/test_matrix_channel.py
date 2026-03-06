import asyncio
from pathlib import Path

from zen_claw.bus.events import OutboundMessage
from zen_claw.bus.queue import MessageBus
from zen_claw.channels.manager import ChannelManager
from zen_claw.channels.matrix import MatrixChannel
from zen_claw.config.schema import Config, MatrixConfig


def test_matrix_channel_start_send_stop() -> None:
    async def _run() -> None:
        ch = MatrixChannel(MatrixConfig(enabled=True, access_token="mock"), MessageBus())

        async def _fake_put(path: str, payload: dict):
            return {"event_id": "$mock"}

        ch._matrix_put = _fake_put  # type: ignore[method-assign]

        await ch.start()
        assert ch.is_running is True
        await ch.send(
            OutboundMessage(channel="matrix", chat_id="!room:matrix.org", content="hello")
        )
        await ch.stop()
        assert ch.is_running is False

    asyncio.run(_run())


def test_matrix_channel_registered_by_manager(tmp_path) -> None:
    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path / "ws")
    cfg.channels.matrix.enabled = True
    mgr = ChannelManager(cfg, MessageBus())
    assert "matrix" in mgr.enabled_channels


def test_matrix_channel_send_payload() -> None:
    async def _run() -> None:
        bus = MessageBus()
        cfg = MatrixConfig(
            enabled=True, homeserver="https://matrix.example.org", access_token="tok"
        )
        ch = MatrixChannel(cfg, bus)
        captured: dict[str, object] = {}

        async def _fake_put(path: str, payload: dict):
            captured["path"] = path
            captured["payload"] = payload
            return {"event_id": "$e1"}

        ch._matrix_put = _fake_put  # type: ignore[method-assign]
        await ch.send(
            OutboundMessage(
                channel="matrix",
                chat_id="!room:matrix.example.org",
                content="hello",
                media=["media://matrix/file/1"],
            )
        )
        assert str(captured["path"]).startswith("/_matrix/client/v3/rooms/")
        payload = captured["payload"]
        assert isinstance(payload, dict)
        assert payload["msgtype"] == "m.text"
        assert "hello" in payload["body"]
        assert "media://matrix/file/1" in payload["body"]

    asyncio.run(_run())


def test_matrix_channel_send_rich_text_and_media_upload() -> None:
    async def _run() -> None:
        bus = MessageBus()
        cfg = MatrixConfig(
            enabled=True, homeserver="https://matrix.example.org", access_token="tok"
        )
        ch = MatrixChannel(cfg, bus)
        calls: list[tuple[str, dict]] = []

        async def _fake_upload(path: str, content: bytes, content_type: str):
            assert path == "/_matrix/media/v3/upload"
            assert content_type == "image/png"
            return {"content_uri": "mxc://matrix.example.org/img1"}

        async def _fake_put(path: str, payload: dict):
            calls.append((path, payload))
            return {"event_id": "$e"}

        media_file = Path("tmp_matrix_img.png")
        media_file.write_bytes(b"\x89PNG\r\n\x1a\n")
        ch._matrix_post_raw = _fake_upload  # type: ignore[method-assign]
        ch._matrix_put = _fake_put  # type: ignore[method-assign]
        try:
            await ch.send(
                OutboundMessage(
                    channel="matrix",
                    chat_id="!room:matrix.example.org",
                    content="hello",
                    media=[str(media_file)],
                    metadata={"matrix_formatted_body": "<b>hello</b>"},
                )
            )
        finally:
            media_file.unlink(missing_ok=True)

        assert len(calls) == 2
        assert calls[0][1]["format"] == "org.matrix.custom.html"
        assert calls[0][1]["formatted_body"] == "<b>hello</b>"
        assert calls[1][1]["msgtype"] == "m.image"
        assert calls[1][1]["url"] == "mxc://matrix.example.org/img1"

    asyncio.run(_run())


def test_matrix_channel_process_sync_response_to_bus() -> None:
    async def _run() -> None:
        bus = MessageBus()
        cfg = MatrixConfig(
            enabled=True, allow_from=["@alice:matrix.org"], user_id="@bot:matrix.org"
        )
        ch = MatrixChannel(cfg, bus)
        data = {
            "next_batch": "s123",
            "rooms": {
                "join": {
                    "!room:matrix.org": {
                        "timeline": {
                            "events": [
                                {
                                    "type": "m.room.message",
                                    "sender": "@bot:matrix.org",
                                    "event_id": "$skip",
                                    "content": {"body": "self"},
                                },
                                {
                                    "type": "m.room.message",
                                    "sender": "@alice:matrix.org",
                                    "event_id": "$ok",
                                    "content": {"body": "hello from matrix"},
                                },
                            ]
                        }
                    }
                }
            },
        }
        await ch._process_sync_response(data)  # type: ignore[attr-defined]
        assert ch._since == "s123"  # type: ignore[attr-defined]
        assert bus.inbound_size == 1
        inbound = await bus.consume_inbound()
        assert inbound.channel == "matrix"
        assert inbound.sender_id == "@alice:matrix.org"
        assert inbound.chat_id == "!room:matrix.org"
        assert inbound.content == "hello from matrix"
        assert inbound.metadata.get("event_id") == "$ok"

    asyncio.run(_run())


def test_matrix_channel_process_media_message_download() -> None:
    async def _run() -> None:
        bus = MessageBus()
        cfg = MatrixConfig(
            enabled=True,
            allow_from=["@alice:matrix.org"],
            user_id="@bot:matrix.org",
            media_download=True,
        )
        ch = MatrixChannel(cfg, bus)

        async def _fake_download(mxc_url: str):
            assert mxc_url == "mxc://matrix.org/media-1"
            return "C:/tmp/matrix-media-1.bin"

        ch._download_mxc = _fake_download  # type: ignore[method-assign]
        data = {
            "next_batch": "s500",
            "rooms": {
                "join": {
                    "!room:matrix.org": {
                        "timeline": {
                            "events": [
                                {
                                    "type": "m.room.message",
                                    "sender": "@alice:matrix.org",
                                    "event_id": "$m1",
                                    "content": {
                                        "msgtype": "m.image",
                                        "body": "photo.png",
                                        "url": "mxc://matrix.org/media-1",
                                    },
                                }
                            ]
                        }
                    }
                }
            },
        }
        await ch._process_sync_response(data)  # type: ignore[attr-defined]
        inbound = await bus.consume_inbound()
        assert inbound.content == "photo.png"
        assert inbound.media == ["C:/tmp/matrix-media-1.bin", "media://matrix/image/media-1"]
        assert inbound.metadata.get("msgtype") == "m.image"

    asyncio.run(_run())


def test_matrix_channel_auto_register_then_sets_token() -> None:
    async def _run() -> None:
        ch = MatrixChannel(
            MatrixConfig(
                enabled=True,
                username="bot",
                password="pw",
                auto_register=True,
                auto_login=True,
            ),
            MessageBus(),
        )

        async def _fake_register(username: str, password: str) -> None:
            assert username == "bot"
            assert password == "pw"
            ch.config.access_token = "tok-r"
            ch.config.user_id = "@bot:matrix.org"
            ch.config.device_id = "DEVR"

        async def _fake_login(_username: str, _password: str) -> None:
            raise AssertionError("login should not be called when register succeeds")

        ch._register = _fake_register  # type: ignore[method-assign]
        ch._login = _fake_login  # type: ignore[method-assign]
        await ch._ensure_access_token()  # type: ignore[attr-defined]
        assert ch.config.access_token == "tok-r"
        assert ch.config.user_id == "@bot:matrix.org"
        assert ch.config.device_id == "DEVR"

    asyncio.run(_run())


def test_matrix_channel_auto_login_after_register_failure() -> None:
    async def _run() -> None:
        ch = MatrixChannel(
            MatrixConfig(
                enabled=True,
                username="bot",
                password="pw",
                auto_register=True,
                auto_login=True,
            ),
            MessageBus(),
        )
        called = {"register": 0, "login": 0}

        async def _fake_register(_username: str, _password: str) -> None:
            called["register"] += 1
            raise RuntimeError("register failed")

        async def _fake_login(_username: str, _password: str) -> None:
            called["login"] += 1
            ch.config.access_token = "tok-l"

        ch._register = _fake_register  # type: ignore[method-assign]
        ch._login = _fake_login  # type: ignore[method-assign]
        await ch._ensure_access_token()  # type: ignore[attr-defined]
        assert called == {"register": 1, "login": 1}
        assert ch.config.access_token == "tok-l"

    asyncio.run(_run())


def test_matrix_channel_send_encrypted_payload_path() -> None:
    async def _run() -> None:
        bus = MessageBus()
        cfg = MatrixConfig(enabled=True, access_token="tok", e2ee_enabled=True)
        ch = MatrixChannel(cfg, bus)
        captured: dict[str, object] = {}

        async def _fake_put(path: str, payload: dict):
            captured["path"] = path
            captured["payload"] = payload
            return {"event_id": "$enc"}

        ch._matrix_put = _fake_put  # type: ignore[method-assign]
        await ch.send(
            OutboundMessage(
                channel="matrix",
                chat_id="!room:matrix.example.org",
                content="plain text ignored when encrypted payload exists",
                metadata={
                    "matrix_encrypted_content": {
                        "algorithm": "m.megolm.v1.aes-sha2",
                        "ciphertext": {"AAA": {"body": "xxx", "type": 0}},
                        "device_id": "DEV1",
                        "sender_key": "KEY1",
                        "session_id": "SID1",
                    }
                },
            )
        )
        assert "/send/m.room.encrypted/" in str(captured["path"])
        assert isinstance(captured["payload"], dict)
        assert captured["payload"]["algorithm"] == "m.megolm.v1.aes-sha2"

    asyncio.run(_run())


def test_matrix_channel_process_encrypted_event_to_bus() -> None:
    async def _run() -> None:
        bus = MessageBus()
        cfg = MatrixConfig(
            enabled=True, allow_from=["@alice:matrix.org"], user_id="@bot:matrix.org"
        )
        ch = MatrixChannel(cfg, bus)
        data = {
            "next_batch": "s234",
            "rooms": {
                "join": {
                    "!room:matrix.org": {
                        "timeline": {
                            "events": [
                                {
                                    "type": "m.room.encrypted",
                                    "sender": "@alice:matrix.org",
                                    "event_id": "$enc",
                                    "content": {
                                        "algorithm": "m.megolm.v1.aes-sha2",
                                        "device_id": "DEV2",
                                        "sender_key": "KEY2",
                                    },
                                }
                            ]
                        }
                    }
                }
            },
        }
        await ch._process_sync_response(data)  # type: ignore[attr-defined]
        inbound = await bus.consume_inbound()
        assert inbound.content == "[encrypted message]"
        assert inbound.metadata.get("encrypted") is True
        assert inbound.metadata.get("algorithm") == "m.megolm.v1.aes-sha2"
        assert inbound.metadata.get("device_id") == "DEV2"

    asyncio.run(_run())

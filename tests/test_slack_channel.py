import asyncio
import hashlib
import hmac
import sys
import time
import types

from zen_claw.bus.events import OutboundMessage
from zen_claw.bus.queue import MessageBus
from zen_claw.channels.slack import SlackChannel
from zen_claw.config.schema import SlackConfig


def test_slack_channel_passive_mode_without_sdk() -> None:
    async def _run() -> None:
        ch = SlackChannel(SlackConfig(enabled=True, bot_token="x"), MessageBus())
        await ch.start()
        assert ch.is_running is True
        await ch.send(OutboundMessage(channel="slack", chat_id="C1", content="hello"))
        await ch.stop()
        assert ch.is_running is False

    asyncio.run(_run())


def test_slack_channel_uses_async_client_when_sdk_present(monkeypatch) -> None:
    sent: list[dict] = []

    class _FakeClient:
        def __init__(self, token: str):
            self.token = token
            self.uploaded: list[tuple[str, str]] = []

        async def chat_postMessage(self, **kwargs):  # noqa: N802
            sent.append(kwargs)

        async def files_upload_v2(self, **kwargs):
            self.uploaded.append((kwargs.get("channel", ""), kwargs.get("title", "")))

    mod = types.ModuleType("slack_sdk.web.async_client")
    mod.AsyncWebClient = _FakeClient
    monkeypatch.setitem(sys.modules, "slack_sdk.web.async_client", mod)

    async def _run() -> None:
        ch = SlackChannel(SlackConfig(enabled=True, bot_token="xoxb-test"), MessageBus())
        await ch.start()
        await ch.send(OutboundMessage(channel="slack", chat_id="C2", content="ping"))
        assert sent and sent[0]["channel"] == "C2"
        assert sent[0]["text"] == "ping"
        assert isinstance(sent[0].get("blocks"), list)
        await ch.stop()

    asyncio.run(_run())


def test_slack_channel_ingest_event_with_file(monkeypatch, tmp_path) -> None:
    class _FakeResp:
        def __init__(self, content: bytes):
            self.content = content

        def raise_for_status(self):
            return None

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None):
            return _FakeResp(b"file")

    monkeypatch.setattr(
        "zen_claw.channels.slack.httpx.AsyncClient", lambda timeout=30.0: _FakeClient()
    )

    async def _run() -> None:
        bus = MessageBus()
        ch = SlackChannel(
            SlackConfig(enabled=True, bot_token="xoxb-test"), bus, media_root=tmp_path
        )
        ch.access_checker = lambda *_args, **_kwargs: True
        await ch.ingest_event(
            {
                "type": "message",
                "user": "U1",
                "channel": "C1",
                "text": "hello",
                "ts": "1.0",
                "files": [
                    {
                        "id": "F1",
                        "name": "a.txt",
                        "mimetype": "text/plain",
                        "url_private_download": "https://example.com/file",
                    }
                ],
            }
        )
        inbound = await bus.consume_inbound()
        assert inbound.channel == "slack"
        assert inbound.chat_id == "C1"
        assert "media_ref" in inbound.content
        assert inbound.media

    asyncio.run(_run())


def test_slack_http_event_signature() -> None:
    async def _run() -> None:
        cfg = SlackConfig(enabled=True, signing_secret="s1")
        ch = SlackChannel(cfg, MessageBus())
        body = b'{"type":"url_verification","challenge":"abc"}'
        ts = str(int(time.time()))
        base = f"v0:{ts}:".encode("utf-8") + body
        sig = "v0=" + hmac.new(b"s1", base, hashlib.sha256).hexdigest()
        out = await ch.handle_http_event(
            body,
            {"x-slack-request-timestamp": ts, "x-slack-signature": sig},
        )
        assert out["ok"] is True
        assert out["challenge"] == "abc"

    asyncio.run(_run())

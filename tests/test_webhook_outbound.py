"""Tests for B3 — Outbound webhook dispatcher."""

from __future__ import annotations

import hashlib
import hmac
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

from zen_claw.webhooks.outbound import WebhookDispatcher, WebhookDispatchRecord

# ── helpers ───────────────────────────────────────────────────────────────────

class _FakeServer:
    """Minimal HTTP server that records received requests."""

    def __init__(self, status: int = 200):
        self._status = status
        self.received: list[dict] = []
        self._server: HTTPServer | None = None

    def start(self) -> str:
        received = self.received
        status = self._status

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                received.append({
                    "body": json.loads(body),
                    "headers": dict(self.headers),
                })
                self.send_response(status)
                self.end_headers()

            def log_message(self, *args):  # silence output
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        port = self._server.server_address[1]
        Thread(target=self._server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{port}"

    def stop(self):
        if self._server:
            self._server.shutdown()


# ── record dataclass ──────────────────────────────────────────────────────────

def test_record_to_dict_roundtrip():
    r = WebhookDispatchRecord(
        at_ms=1000, url="http://x", agent_id="a", session_id="s",
        intent="test", status_code=200, attempts=1, success=True, error="",
    )
    d = r.to_dict()
    r2 = WebhookDispatchRecord.from_dict(d)
    assert r2.url == r.url
    assert r2.success is True
    assert r2.attempts == 1


def test_record_failed_default():
    r = WebhookDispatchRecord(
        at_ms=0, url="", agent_id="", session_id="",
        intent="", status_code=0, attempts=3, success=False, error="timeout",
    )
    assert r.success is False
    assert r.error == "timeout"


# ── dispatcher: fail-closed without sidecar ──────────────────────────────────

def test_dispatch_requires_sidecar_configuration(tmp_path: Path):
    dispatcher = WebhookDispatcher(tmp_path)
    record = dispatcher.dispatch("https://hooks.example.test/run", agent_id="bot", session_id="s1", intent="greet")
    assert record.success is False
    assert record.status_code == 0
    assert record.error_code == "webhook_sidecar_not_configured"
    assert "sidecar configuration" in record.error


def test_dispatch_writes_audit_log(tmp_path: Path):
    dispatcher = WebhookDispatcher(tmp_path)
    dispatcher.dispatch("https://hooks.example.test/run", agent_id="bot2")
    log_path = tmp_path / WebhookDispatcher.LOG_FILENAME
    assert log_path.exists()
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["success"] is False
    assert entry["error_code"] == "webhook_sidecar_not_configured"


# ── dispatcher: sidecar execution ─────────────────────────────────────────────

def test_dispatch_via_sidecar_records_sidecar_target(monkeypatch, tmp_path: Path):
    captured: dict[str, object] = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True, "status": 202, "body": {"queued": True}}

    def _fake_post(url, headers=None, content=None, json=None, timeout=None):  # noqa: ANN001
        captured["url"] = url
        captured["headers"] = headers
        captured["content"] = content
        captured["json"] = json
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr("zen_claw.webhooks.outbound.httpx.post", _fake_post)
    dispatcher = WebhookDispatcher(
        tmp_path,
        connector_proxy_url="http://127.0.0.1:4499/v1/fetch",
        connector_approval_token="token-1",
    )
    record = dispatcher.dispatch(
        "https://hooks.example.test/run",
        agent_id="bot",
        trace_id="trace-1",
        tenant_id="tenant-a",
        workspace_id="ws-1",
        agent_profile="ops",
    )
    assert record.success is True
    assert record.sidecar_target == "http://127.0.0.1:4499/v1/fetch"
    assert record.trace_id == "trace-1"
    assert captured["url"] == "http://127.0.0.1:4499/v1/fetch"
    assert captured["content"] is None or captured["json"] is not None


# ── HMAC signing ─────────────────────────────────────────────────────────────

def test_dispatch_via_sidecar_includes_signature_header(monkeypatch, tmp_path: Path):
    captured: dict[str, object] = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True, "status": 202, "body": {"queued": True}}

    def _fake_post(url, headers=None, content=None, json=None, timeout=None):  # noqa: ANN001
        captured["url"] = url
        captured["headers"] = headers
        captured["content"] = content
        captured["json"] = json
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr("zen_claw.webhooks.outbound.httpx.post", _fake_post)
    dispatcher = WebhookDispatcher(
        tmp_path,
        secret="mysecret",
        connector_proxy_url="http://127.0.0.1:4499/v1/fetch",
        connector_approval_token="token-1",
    )
    dispatcher.dispatch("https://hooks.example.test/run", agent_id="bot")
    request_payload = dict(captured["json"] or {})
    outbound_headers = dict(request_payload["headers"])
    assert "X-Webhook-Signature" in outbound_headers


def test_dispatch_via_sidecar_signature_is_valid_hmac(monkeypatch, tmp_path: Path):
    captured: dict[str, object] = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True, "status": 202, "body": {"queued": True}}

    def _fake_post(url, headers=None, content=None, json=None, timeout=None):  # noqa: ANN001
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr("zen_claw.webhooks.outbound.httpx.post", _fake_post)
    secret = "topsecret"
    dispatcher = WebhookDispatcher(
        tmp_path,
        secret=secret,
        connector_proxy_url="http://127.0.0.1:4499/v1/fetch",
        connector_approval_token="token-1",
    )
    dispatcher.dispatch("https://hooks.example.test/run", agent_id="bot")
    req = dict(captured["json"] or {})
    raw_body = str(req["body"]).encode()
    sig_header = dict(req["headers"])["X-Webhook-Signature"]
    assert WebhookDispatcher.verify_signature(raw_body, sig_header, secret)


def test_verify_signature_wrong_secret(tmp_path: Path):
    body = b'{"test": 1}'
    sig = "sha256=" + hmac.new(b"correct", body, hashlib.sha256).hexdigest()
    assert WebhookDispatcher.verify_signature(body, sig, "wrong") is False


def test_verify_signature_bad_format():
    assert WebhookDispatcher.verify_signature(b"x", "no-prefix", "s") is False


# ── read_log ─────────────────────────────────────────────────────────────────

def test_read_log_empty(tmp_path: Path):
    dispatcher = WebhookDispatcher(tmp_path)
    assert dispatcher.read_log() == []


def test_read_log_returns_records(tmp_path: Path):
    dispatcher = WebhookDispatcher(tmp_path)
    dispatcher.dispatch("https://hooks.example.test/run-a", agent_id="a1", intent="foo")
    dispatcher.dispatch("https://hooks.example.test/run-b", agent_id="a2", intent="bar")
    records = dispatcher.read_log()
    assert len(records) == 2
    intents = {r.intent for r in records}
    assert "foo" in intents
    assert "bar" in intents

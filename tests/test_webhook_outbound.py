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


# ── dispatcher: successful dispatch ──────────────────────────────────────────

def test_dispatch_success(tmp_path: Path):
    srv = _FakeServer(status=200)
    url = srv.start()
    try:
        dispatcher = WebhookDispatcher(tmp_path)
        record = dispatcher.dispatch(url, agent_id="bot", session_id="s1", intent="greet")
        assert record.success is True
        assert record.status_code == 200
        assert record.attempts == 1
        assert len(srv.received) == 1
        assert srv.received[0]["body"]["agent_id"] == "bot"
    finally:
        srv.stop()


def test_dispatch_writes_audit_log(tmp_path: Path):
    srv = _FakeServer(status=200)
    url = srv.start()
    try:
        dispatcher = WebhookDispatcher(tmp_path)
        dispatcher.dispatch(url, agent_id="bot2")
        log_path = tmp_path / WebhookDispatcher.LOG_FILENAME
        assert log_path.exists()
        lines = log_path.read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["success"] is True
    finally:
        srv.stop()


# ── dispatcher: retry on failure ─────────────────────────────────────────────

def test_dispatch_retries_on_server_error(tmp_path: Path):
    srv = _FakeServer(status=500)
    url = srv.start()
    try:
        dispatcher = WebhookDispatcher(tmp_path, max_retries=2)
        record = dispatcher.dispatch(url, agent_id="bot")
        assert record.attempts == 3          # 1 + 2 retries
        assert record.success is False
        assert record.status_code == 500
    finally:
        srv.stop()


def test_dispatch_no_retry_on_success(tmp_path: Path):
    srv = _FakeServer(status=201)
    url = srv.start()
    try:
        dispatcher = WebhookDispatcher(tmp_path, max_retries=2)
        record = dispatcher.dispatch(url, agent_id="bot")
        assert record.attempts == 1
        assert record.success is True
    finally:
        srv.stop()


def test_dispatch_network_error_records_failure(tmp_path: Path):
    dispatcher = WebhookDispatcher(tmp_path, max_retries=0, timeout_sec=0.1)
    record = dispatcher.dispatch(
        "http://127.0.0.1:1",  # nothing listening → connection refused
        agent_id="bot",
    )
    assert record.success is False
    assert record.status_code == 0
    assert record.attempts == 1


# ── HMAC signing ─────────────────────────────────────────────────────────────

def test_dispatch_includes_signature_header(tmp_path: Path):
    srv = _FakeServer(status=200)
    url = srv.start()
    try:
        dispatcher = WebhookDispatcher(tmp_path, secret="mysecret")
        dispatcher.dispatch(url, agent_id="bot")
        headers = srv.received[0]["headers"]
        assert "x-webhook-signature" in {k.lower() for k in headers}
    finally:
        srv.stop()


def test_dispatch_signature_is_valid_hmac(tmp_path: Path):
    srv = _FakeServer(status=200)
    url = srv.start()
    try:
        secret = "topsecret"
        dispatcher = WebhookDispatcher(tmp_path, secret=secret)
        dispatcher.dispatch(url, agent_id="bot")
        req = srv.received[0]
        raw_body = json.dumps(req["body"], ensure_ascii=False).encode()
        sig_header = next(
            v for k, v in req["headers"].items()
            if k.lower() == "x-webhook-signature"
        )
        assert WebhookDispatcher.verify_signature(raw_body, sig_header, secret)
    finally:
        srv.stop()


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
    srv = _FakeServer(status=200)
    url = srv.start()
    try:
        dispatcher = WebhookDispatcher(tmp_path)
        dispatcher.dispatch(url, agent_id="a1", intent="foo")
        dispatcher.dispatch(url, agent_id="a2", intent="bar")
        records = dispatcher.read_log()
        assert len(records) == 2
        intents = {r.intent for r in records}
        assert "foo" in intents
        assert "bar" in intents
    finally:
        srv.stop()

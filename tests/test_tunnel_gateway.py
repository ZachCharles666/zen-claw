import hashlib
import hmac
import time

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")
from fastapi import HTTPException

from zen_claw.tunnel.gateway import TunnelGatewaySecurity


def test_gateway_security_valid_request():
    gateway = TunnelGatewaySecurity()
    gateway.add_active_key("key-1", "my-super-secret")

    body = b'{"hello": "world"}'
    ts = str(int(time.time()))
    nonce = "unique-nonce-123"

    payload = f"{ts}.{nonce}.".encode("utf-8") + body
    sig = hmac.new("my-super-secret".encode("utf-8"), payload, hashlib.sha256).hexdigest()

    headers = {
        "x-claw-signature": sig,
        "x-claw-timestamp": ts,
        "x-claw-nonce": nonce,
        "x-claw-key-id": "key-1",
    }

    assert gateway.verify_request_sync(body, headers, "127.0.0.1")


def test_gateway_security_invalid_signature():
    gateway = TunnelGatewaySecurity()
    gateway.add_active_key("key-1", "my-super-secret")

    body = b'{"hello": "world"}'
    ts = str(int(time.time()))
    nonce = "unique-nonce-456"

    # Sign with WRONG secret
    payload = f"{ts}.{nonce}.".encode("utf-8") + body
    sig = hmac.new("wrong-secret".encode("utf-8"), payload, hashlib.sha256).hexdigest()

    headers = {
        "x-claw-signature": sig,
        "x-claw-timestamp": ts,
        "x-claw-nonce": nonce,
        "x-claw-key-id": "key-1",
    }

    with pytest.raises(HTTPException) as excinfo:
        gateway.verify_request_sync(body, headers, "127.0.0.1")
    assert excinfo.value.status_code == 401
    assert "Invalid Signature" in excinfo.value.detail


def test_gateway_circuit_breaker():
    gateway = TunnelGatewaySecurity(circuit_breaker_threshold=5)
    gateway.add_active_key("key-1", "my-super-secret")

    body = b'{"attack": "yes"}'

    # Hit limits
    for i in range(5):
        try:
            gateway.verify_request_sync(body, {}, "10.0.0.5")
        except HTTPException:
            pass

    # The 6th request should trip the breaker even before auth
    with pytest.raises(HTTPException) as excinfo:
        gateway.verify_request_sync(body, {}, "10.0.0.5")
    assert excinfo.value.status_code == 429
    assert "Too Many Requests" in excinfo.value.detail


def test_gateway_nonce_replay():
    gateway = TunnelGatewaySecurity()
    gateway.add_active_key("key-1", "my-secret")

    body = b"{}"
    ts = str(int(time.time()))
    nonce = "replay-nonce"

    payload = f"{ts}.{nonce}.".encode("utf-8") + body
    sig = hmac.new(b"my-secret", payload, hashlib.sha256).hexdigest()

    headers = {
        "x-claw-signature": sig,
        "x-claw-timestamp": ts,
        "x-claw-nonce": nonce,
        "x-claw-key-id": "key-1",
    }

    # First time works
    assert gateway.verify_request_sync(body, headers, "127.0.0.1")

    # Second time fails
    with pytest.raises(HTTPException) as excinfo:
        gateway.verify_request_sync(body, headers, "127.0.0.1")
    assert excinfo.value.status_code == 401
    assert "Nonce already used" in excinfo.value.detail

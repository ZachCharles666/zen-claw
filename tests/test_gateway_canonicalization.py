from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from zen_claw.security_context import (
    canonical_gateway_envelope,
    canonical_gateway_envelope_bytes,
    canonical_gateway_envelope_hash,
    sign_gateway_envelope,
    verify_gateway_envelope,
)

FIXTURES_PATH = Path("tests/fixtures/gateway_canonicalization_golden.json")


def _load_fixtures() -> list[dict]:
    return json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))


def test_python_gateway_canonicalization_golden_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZEN_CLAW_HMAC_MASTER_KEY", "fixture-master-key")
    for fixture in _load_fixtures():
        payload = fixture["payload"]
        assert canonical_gateway_envelope(payload) == fixture["canonical_payload"]
        assert canonical_gateway_envelope_bytes(payload).decode("utf-8") == fixture["canonical_text"]
        assert canonical_gateway_envelope_hash(payload) == fixture["request_hash"]
        assert sign_gateway_envelope(payload) == fixture["gateway_signature"]
        ok, error = verify_gateway_envelope(payload)
        assert ok is True, error


def test_python_gateway_canonicalization_detects_tampering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZEN_CLAW_HMAC_MASTER_KEY", "fixture-master-key")
    fixture = _load_fixtures()[0]
    payload = json.loads(json.dumps(fixture["payload"]))
    payload["gateway_meta"]["request_hash"] = "0" * 64
    ok, error = verify_gateway_envelope(payload)
    assert ok is False
    assert error == "request_hash_mismatch"

    payload = json.loads(json.dumps(fixture["payload"]))
    payload["gateway_signature"] = "0" * 64
    ok, error = verify_gateway_envelope(payload)
    assert ok is False
    assert error == "signature_invalid"


def test_node_gateway_canonicalization_golden_fixtures() -> None:
    node = shutil.which("node.exe" if os.name == "nt" else "node")
    if not node:
        pytest.skip("node not installed")
    result = subprocess.run(
        [node, "canonicalization.test.js"],
        cwd="browser/sidecar",
        env={**os.environ, "ZEN_CLAW_HMAC_MASTER_KEY": "fixture-master-key"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_go_gateway_canonicalization_golden_fixtures() -> None:
    go = shutil.which("go.exe" if os.name == "nt" else "go")
    if not go:
        pytest.skip("go not installed")
    cache_root = Path(".pytest_cache") / "go-build"
    cache_root.mkdir(parents=True, exist_ok=True)
    for workdir in ("go/net-proxy", "go/sec-execd"):
        env = os.environ.copy()
        env["GOCACHE"] = tempfile.mkdtemp(prefix="go-cache-", dir=str(cache_root))
        env["ZEN_CLAW_HMAC_MASTER_KEY"] = "fixture-master-key"
        result = subprocess.run(
            [go, "test", "-run", "TestCanonicalGatewayPayloadFixtures", "."],
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"{workdir}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"

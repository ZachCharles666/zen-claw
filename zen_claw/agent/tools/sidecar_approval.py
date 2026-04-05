"""Request-scoped sidecar approval helpers."""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Any

from zen_claw.security_context import gateway_instance_id, stable_json_hash


def _build_signature_fields(
    *,
    trace_id: str,
    ts: int,
    nonce: str,
    method: str,
    path: str,
    body_bytes: bytes,
    gateway_instance: str,
    policy_snapshot_hash: str,
) -> tuple[str, str]:
    body_hash = hashlib.sha256(body_bytes).hexdigest()
    canonical = "\n".join(
        [
            trace_id.strip(),
            str(ts),
            nonce.strip(),
            method.upper().strip(),
            path.strip(),
            body_hash,
            gateway_instance.strip(),
            policy_snapshot_hash.strip(),
        ]
    )
    return canonical, body_hash

def build_hmac_approval_headers(
    *,
    secret: str,
    trace_id: str,
    method: str,
    path: str,
    body_bytes: bytes,
    policy_snapshot: dict[str, Any] | None = None,
    gateway_instance: str | None = None,
    now_ts: int | None = None,
) -> dict[str, str]:
    ts = int(time.time()) if now_ts is None else int(now_ts)
    nonce = hashlib.sha256(
        f"{trace_id}:{ts}:{method}:{path}:{os.getpid()}".encode("utf-8")
    ).hexdigest()[:16]
    snapshot = dict(policy_snapshot or {})
    snapshot_hash = str(snapshot.get("policy_snapshot_hash") or stable_json_hash(snapshot))
    instance = str(gateway_instance or gateway_instance_id()).strip()
    canonical, body_hash = _build_signature_fields(
        trace_id=trace_id,
        ts=ts,
        nonce=nonce,
        method=method,
        path=path,
        body_bytes=body_bytes,
        gateway_instance=instance,
        policy_snapshot_hash=snapshot_hash,
    )
    sig = hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "X-Trace-Id": trace_id,
        "X-Gateway-Timestamp": str(ts),
        "X-Gateway-Nonce": nonce,
        "X-Gateway-Request-Hash": body_hash,
        "X-Gateway-Instance": instance,
        "X-Policy-Snapshot-Hash": snapshot_hash,
        "X-Gateway-Signature": sig,
        "X-Approval-Timestamp": str(ts),
        "X-Approval-Signature": sig,
    }


def hmac_body_json(payload: dict[str, Any]) -> bytes:
    # Keep stable ordering for deterministic signatures.
    import json

    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")

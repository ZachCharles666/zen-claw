"""Request-scoped sidecar approval helpers."""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any


def build_hmac_approval_headers(
    *,
    secret: str,
    trace_id: str,
    method: str,
    path: str,
    body_bytes: bytes,
    now_ts: int | None = None,
) -> dict[str, str]:
    ts = int(time.time()) if now_ts is None else int(now_ts)
    body_hash = hashlib.sha256(body_bytes).hexdigest()
    canonical = "\n".join(
        [
            trace_id.strip(),
            str(ts),
            method.upper().strip(),
            path.strip(),
            body_hash,
        ]
    ).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    return {
        "X-Trace-Id": trace_id,
        "X-Approval-Timestamp": str(ts),
        "X-Approval-Signature": sig,
    }


def hmac_body_json(payload: dict[str, Any]) -> bytes:
    # Keep stable ordering for deterministic signatures.
    import json

    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")

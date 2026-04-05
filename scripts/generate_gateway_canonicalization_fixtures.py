from __future__ import annotations

import json
from pathlib import Path

from zen_claw.security_context import (
    canonical_gateway_envelope,
    canonical_gateway_envelope_bytes,
    canonical_gateway_envelope_hash,
    canonical_policy_snapshot_hash,
    sign_gateway_envelope,
)

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "tests" / "fixtures" / "gateway_canonicalization_cases.json"
GOLDEN_PATH = ROOT / "tests" / "fixtures" / "gateway_canonicalization_golden.json"


def _normalize_policy_hashes(payload: dict) -> dict:
    normalized = json.loads(json.dumps(payload))
    policy_snapshot = dict(normalized.get("policy_snapshot") or {})
    policy_snapshot_hash = str(
        normalized.get("policy_snapshot_hash")
        or policy_snapshot.get("policy_snapshot_hash")
        or canonical_policy_snapshot_hash(policy_snapshot)
    )
    policy_snapshot["policy_snapshot_hash"] = policy_snapshot_hash
    normalized["policy_snapshot"] = policy_snapshot
    normalized["policy_snapshot_hash"] = policy_snapshot_hash
    security_context = dict(normalized.get("security_context") or {})
    if security_context:
        security_context["policy_snapshot"] = dict(
            security_context.get("policy_snapshot") or policy_snapshot
        )
        security_context["policy_snapshot_hash"] = str(
            security_context.get("policy_snapshot_hash") or policy_snapshot_hash
        )
        normalized["security_context"] = security_context
    gateway_meta = dict(normalized.get("gateway_meta") or {})
    gateway_meta["policy_snapshot_hash"] = str(
        gateway_meta.get("policy_snapshot_hash") or policy_snapshot_hash
    )
    normalized["gateway_meta"] = gateway_meta
    return normalized


def main() -> None:
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    golden: list[dict] = []
    for case in cases:
        payload = _normalize_policy_hashes(case["payload"])
        canonical = canonical_gateway_envelope(payload)
        request_hash = canonical_gateway_envelope_hash(payload)
        signed_payload = json.loads(json.dumps(payload))
        signed_payload.setdefault("gateway_meta", {})
        signed_payload["gateway_meta"]["request_hash"] = request_hash
        signed_payload["gateway_signature"] = sign_gateway_envelope(signed_payload)
        golden.append(
            {
                "name": case["name"],
                "payload": signed_payload,
                "canonical_payload": canonical,
                "canonical_text": canonical_gateway_envelope_bytes(payload).decode("utf-8"),
                "request_hash": request_hash,
                "gateway_signature": signed_payload["gateway_signature"],
            }
        )
    GOLDEN_PATH.write_text(
        json.dumps(golden, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()

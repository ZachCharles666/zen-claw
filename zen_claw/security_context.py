"""Unified zero-trust gateway envelope issuance and signing helpers."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from zen_claw.utils.crypto import sign_data, verify_signature

GATEWAY_ENVELOPE_VERSION = 1


def stable_json_bytes(payload: Any) -> bytes:
    """Serialize payload deterministically for hashing/signing."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def stable_json_hash(payload: Any) -> str:
    """Return sha256 hex digest for a JSON-serializable payload."""
    return hashlib.sha256(stable_json_bytes(payload)).hexdigest()


def gateway_instance_id() -> str:
    """Return a stable gateway instance identifier."""
    raw = str(
        os.environ.get("ZEN_CLAW_GATEWAY_INSTANCE_ID")
        or os.environ.get("HOSTNAME")
        or os.environ.get("COMPUTERNAME")
        or "local-gateway"
    ).strip()
    return raw or "local-gateway"


@dataclass(frozen=True)
class GatewaySubject:
    """Canonical zero-trust subject identity for gateway issuance."""

    trace_id: str
    channel: str
    sender_id: str
    chat_id: str
    tenant_id: str
    workspace_id: str
    agent_profile: str
    role: str
    trust_level: str
    origin_surface: str
    channel_role: str = ""
    workspace_path: str = ""
    gateway_instance: str = ""
    dev_profile: bool = False
    trusted_local_only: bool = False
    subject_type: str = "agent"
    trust_tier: str = ""
    capability_grants: tuple[str, ...] = ()

    def security_context(self, *, policy_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = asdict(self)
        payload["policy_snapshot"] = dict(policy_snapshot or {})
        payload["policy_snapshot_hash"] = stable_json_hash(payload["policy_snapshot"])
        payload["subject_type"] = str(self.subject_type or "agent").strip().lower() or "agent"
        payload["trust_tier"] = (
            str(self.trust_tier or self.trust_level or "").strip().lower()
        )
        payload["capability_grants"] = list(self.capability_grants)
        return payload


class GatewayEnvelopeIssuer:
    """Single issuer for signed gateway envelopes."""

    def __init__(self, *, gateway_instance: str | None = None) -> None:
        self._gateway_instance = str(gateway_instance or gateway_instance_id()).strip()

    def issue(
        self,
        *,
        subject: GatewaySubject,
        policy_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        sec = subject.security_context(policy_snapshot=policy_snapshot)
        sec["gateway_instance"] = str(
            sec.get("gateway_instance") or self._gateway_instance or gateway_instance_id()
        ).strip()
        policy = dict(sec.get("policy_snapshot") or {})
        if "policy_snapshot_hash" not in policy:
            policy["policy_snapshot_hash"] = stable_json_hash(policy)
        policy_snapshot_hash = str(policy.get("policy_snapshot_hash") or stable_json_hash(policy))
        envelope = {
            "trace_id": str(sec.get("trace_id") or "").strip(),
            "security_context": {
                **sec,
                "policy_snapshot": policy,
                "policy_snapshot_hash": policy_snapshot_hash,
            },
            "policy_snapshot": policy,
            "policy_snapshot_hash": policy_snapshot_hash,
            "gateway_meta": {
                "gateway_instance": str(
                    sec.get("gateway_instance") or self._gateway_instance or gateway_instance_id()
                ).strip(),
                "policy_snapshot_hash": policy_snapshot_hash,
                "envelope_version": GATEWAY_ENVELOPE_VERSION,
            },
            "subject_type": str(sec.get("subject_type") or "agent").strip().lower() or "agent",
            "trust_tier": str(sec.get("trust_tier") or "").strip().lower(),
            "capability_grants": list(sec.get("capability_grants") or []),
        }
        flattened = {
            **dict(envelope["security_context"]),
            "policy_snapshot": dict(policy),
            "policy_snapshot_hash": policy_snapshot_hash,
            "gateway_instance": str(envelope["gateway_meta"]["gateway_instance"]),
            "subject_type": envelope["subject_type"],
            "trust_tier": envelope["trust_tier"],
            "capability_grants": list(envelope["capability_grants"]),
        }
        envelope.update(flattened)
        envelope["gateway_meta"]["request_hash"] = stable_json_hash(_canonical_gateway_envelope(envelope))
        envelope["gateway_signature"] = sign_gateway_envelope(envelope)
        return envelope


def _canonical_gateway_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    canonical = dict(envelope)
    canonical.pop("gateway_signature", None)
    gateway_meta = canonical.get("gateway_meta")
    if isinstance(gateway_meta, dict):
        canonical["gateway_meta"] = dict(gateway_meta)
        canonical["gateway_meta"].pop("request_hash", None)
    return canonical


def sign_gateway_envelope(envelope: dict[str, Any]) -> str:
    """Return gateway envelope signature."""
    return sign_data(stable_json_bytes(_canonical_gateway_envelope(envelope)).decode("utf-8"))


def verify_gateway_envelope(envelope: dict[str, Any]) -> tuple[bool, str]:
    """Verify gateway request hash and signature integrity."""
    if not isinstance(envelope, dict):
        return False, "envelope_not_object"
    gateway_signature = str(envelope.get("gateway_signature") or "").strip()
    gateway_meta = dict(envelope.get("gateway_meta") or {})
    request_hash = str(gateway_meta.get("request_hash") or "").strip()
    expected_hash = stable_json_hash(_canonical_gateway_envelope(envelope))
    if not gateway_signature:
        return False, "signature_missing"
    if not request_hash:
        return False, "request_hash_missing"
    if request_hash != expected_hash:
        return False, "request_hash_mismatch"
    canonical = stable_json_bytes(_canonical_gateway_envelope(envelope)).decode("utf-8")
    if not verify_signature(canonical, gateway_signature):
        return False, "signature_invalid"
    return True, ""


def extract_security_context(envelope_or_context: dict[str, Any] | None) -> dict[str, Any]:
    """Return normalized inner security_context from either envelope or legacy flat context."""
    raw = dict(envelope_or_context or {})
    sec = dict(raw.get("security_context") or {})
    if sec:
        return sec
    legacy_keys = {
        "trace_id",
        "channel",
        "sender_id",
        "chat_id",
        "tenant_id",
        "workspace_id",
        "agent_profile",
        "role",
        "trust_level",
        "origin_surface",
        "channel_role",
        "workspace_path",
        "gateway_instance",
        "dev_profile",
        "trusted_local_only",
        "policy_snapshot",
        "policy_snapshot_hash",
        "subject_type",
        "trust_tier",
        "capability_grants",
    }
    return {key: raw[key] for key in legacy_keys if key in raw}


def ensure_security_envelope(
    envelope_or_context: dict[str, Any] | None,
    *,
    trace_id: str = "",
    subject_type: str = "",
    trust_tier: str = "",
    capability_grants: list[str] | None = None,
) -> dict[str, Any]:
    """Normalize a legacy flat context or partial envelope into a signed gateway envelope."""
    raw = dict(envelope_or_context or {})
    sec = extract_security_context(raw)
    sec_trace_id = str(trace_id or sec.get("trace_id") or raw.get("trace_id") or "").strip()
    policy_snapshot = dict(
        raw.get("policy_snapshot")
        or sec.get("policy_snapshot")
        or raw.get("security_context", {}).get("policy_snapshot", {})
        or {}
    )
    if policy_snapshot and "policy_snapshot_hash" not in policy_snapshot:
        policy_snapshot["policy_snapshot_hash"] = stable_json_hash(policy_snapshot)
    grants = capability_grants
    if grants is None:
        if "capability_grants" in raw:
            grants = raw.get("capability_grants")
        elif "capability_grants" in sec:
            grants = sec.get("capability_grants")
        else:
            grants = ["*"]
    issuer = GatewayEnvelopeIssuer(
        gateway_instance=str(
            raw.get("gateway_meta", {}).get("gateway_instance")
            or sec.get("gateway_instance")
            or raw.get("gateway_instance")
            or gateway_instance_id()
        ).strip()
    )
    subject = GatewaySubject(
        trace_id=sec_trace_id,
        channel=str(sec.get("channel") or "").strip().lower(),
        sender_id=str(sec.get("sender_id") or "").strip(),
        chat_id=str(sec.get("chat_id") or "").strip(),
        tenant_id=str(sec.get("tenant_id") or "default").strip().lower() or "default",
        workspace_id=str(sec.get("workspace_id") or "").strip(),
        agent_profile=str(sec.get("agent_profile") or "default").strip().lower() or "default",
        role=str(sec.get("role") or "").strip().lower(),
        trust_level=str(sec.get("trust_level") or "trusted_local").strip().lower()
        or "trusted_local",
        origin_surface=str(sec.get("origin_surface") or sec.get("channel") or "").strip().lower(),
        channel_role=str(sec.get("channel_role") or "").strip().lower(),
        workspace_path=str(sec.get("workspace_path") or "").strip(),
        gateway_instance=str(
            sec.get("gateway_instance") or raw.get("gateway_instance") or gateway_instance_id()
        ).strip(),
        dev_profile=bool(sec.get("dev_profile")),
        trusted_local_only=bool(sec.get("trusted_local_only")),
        subject_type=(
            str(subject_type or raw.get("subject_type") or sec.get("subject_type") or "agent")
            .strip()
            .lower()
            or "agent"
        ),
        trust_tier=(
            str(trust_tier or raw.get("trust_tier") or sec.get("trust_tier") or sec.get("trust_level") or "")
            .strip()
            .lower()
        ),
        capability_grants=tuple(str(v).strip().lower() for v in (grants or []) if str(v).strip()),
    )
    return issuer.issue(subject=subject, policy_snapshot=policy_snapshot)


@dataclass(frozen=True)
class SecurityContext:
    """Normalized request identity/boundary context."""

    trace_id: str
    channel: str
    sender_id: str
    chat_id: str
    tenant_id: str
    workspace_id: str
    agent_profile: str
    role: str
    trust_level: str
    origin_surface: str
    channel_role: str = ""
    workspace_path: str = ""
    gateway_instance: str = ""
    dev_profile: bool = False
    trusted_local_only: bool = False
    policy_snapshot: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["policy_snapshot"] = dict(self.policy_snapshot or {})
        payload["policy_snapshot_hash"] = stable_json_hash(payload["policy_snapshot"])
        return payload


def security_policy_snapshot(
    *,
    production_hardening: bool,
    legacy_compat: bool,
    restrict_to_workspace: bool,
    profile_allowed_tools: list[str] | None = None,
    profile_denied_tools: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build canonical policy snapshot attached to each request."""
    snapshot = {
        "production_hardening": bool(production_hardening),
        "legacy_compat": bool(legacy_compat),
        "restrict_to_workspace": bool(restrict_to_workspace),
        "profile_allowed_tools": list(profile_allowed_tools or []),
        "profile_denied_tools": list(profile_denied_tools or []),
    }
    for key, value in (extra or {}).items():
        snapshot[key] = value
    snapshot["policy_snapshot_hash"] = stable_json_hash(snapshot)
    return snapshot


def build_security_context(
    *,
    trace_id: str,
    channel: str,
    sender_id: str,
    chat_id: str,
    tenant_id: str = "default",
    workspace_id: str = "",
    agent_profile: str = "default",
    role: str = "",
    trust_level: str = "trusted_local",
    origin_surface: str = "",
    channel_role: str = "",
    workspace_path: str = "",
    gateway_instance: str | None = None,
    dev_profile: bool = False,
    trusted_local_only: bool = False,
    policy_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a signed gateway security envelope."""
    issuer = GatewayEnvelopeIssuer(gateway_instance=str(gateway_instance or gateway_instance_id()).strip())
    subject = GatewaySubject(
        trace_id=str(trace_id or "").strip(),
        channel=str(channel or "").strip().lower(),
        sender_id=str(sender_id or "").strip(),
        chat_id=str(chat_id or "").strip(),
        tenant_id=str(tenant_id or "default").strip().lower() or "default",
        workspace_id=str(workspace_id or "").strip(),
        agent_profile=str(agent_profile or "default").strip().lower() or "default",
        role=str(role or "").strip().lower(),
        trust_level=str(trust_level or "").strip().lower() or "trusted_local",
        origin_surface=str(origin_surface or channel or "").strip().lower(),
        channel_role=str(channel_role or "").strip().lower(),
        workspace_path=str(workspace_path or "").strip(),
        gateway_instance=str(gateway_instance or gateway_instance_id()).strip(),
        dev_profile=bool(dev_profile),
        trusted_local_only=bool(trusted_local_only),
    )
    return issuer.issue(subject=subject, policy_snapshot=dict(policy_snapshot or {}))


def issue_gateway_envelope(
    *,
    trace_id: str,
    channel: str,
    sender_id: str,
    chat_id: str,
    tenant_id: str = "default",
    workspace_id: str = "",
    agent_profile: str = "default",
    role: str = "",
    trust_level: str = "trusted_local",
    origin_surface: str = "",
    channel_role: str = "",
    workspace_path: str = "",
    gateway_instance: str | None = None,
    dev_profile: bool = False,
    trusted_local_only: bool = False,
    policy_snapshot: dict[str, Any] | None = None,
    subject_type: str = "agent",
    trust_tier: str = "",
    capability_grants: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Explicit single-entry API for issuing a signed gateway envelope."""
    issuer = GatewayEnvelopeIssuer(gateway_instance=gateway_instance)
    subject = GatewaySubject(
        trace_id=str(trace_id or "").strip(),
        channel=str(channel or "").strip().lower(),
        sender_id=str(sender_id or "").strip(),
        chat_id=str(chat_id or "").strip(),
        tenant_id=str(tenant_id or "default").strip().lower() or "default",
        workspace_id=str(workspace_id or "").strip(),
        agent_profile=str(agent_profile or "default").strip().lower() or "default",
        role=str(role or "").strip().lower(),
        trust_level=str(trust_level or "").strip().lower() or "trusted_local",
        origin_surface=str(origin_surface or channel or "").strip().lower(),
        channel_role=str(channel_role or "").strip().lower(),
        workspace_path=str(workspace_path or "").strip(),
        gateway_instance=str(gateway_instance or gateway_instance_id()).strip(),
        dev_profile=bool(dev_profile),
        trusted_local_only=bool(trusted_local_only),
        subject_type=str(subject_type or "agent").strip().lower() or "agent",
        trust_tier=str(trust_tier or trust_level or "").strip().lower(),
        capability_grants=tuple(
            str(item).strip().lower() for item in (capability_grants or []) if str(item).strip()
        ),
    )
    return issuer.issue(subject=subject, policy_snapshot=dict(policy_snapshot or {}))


def resource_scope_for_security_context(
    security_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Project the request context into a stable resource scope."""
    ctx = extract_security_context(security_context)
    return {
        "tenant_id": str(ctx.get("tenant_id") or "").strip().lower(),
        "workspace_id": str(ctx.get("workspace_id") or "").strip(),
        "channel": str(ctx.get("channel") or "").strip().lower(),
        "chat_id": str(ctx.get("chat_id") or "").strip(),
        "agent_profile": str(ctx.get("agent_profile") or "").strip().lower(),
        "trust_level": str(ctx.get("trust_level") or "").strip().lower(),
        "origin_surface": str(ctx.get("origin_surface") or "").strip().lower(),
    }


def is_trusted_local_surface(channel_name: str, trusted_local_channels: list[str] | set[str]) -> bool:
    """Return whether a surface is explicitly trusted-local."""
    channel = str(channel_name or "").strip().lower()
    allow = {str(v).strip().lower() for v in trusted_local_channels if str(v).strip()}
    return bool(channel) and channel in allow


def normalize_workspace_id(workspace_path: str | Path) -> str:
    """Return stable workspace identifier from path."""
    path = Path(workspace_path).expanduser()
    return path.name or str(path)

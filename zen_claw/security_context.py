"""Unified zero-trust security context and signing helpers."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


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
    """Return a normalized security context dict."""
    ctx = SecurityContext(
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
        policy_snapshot=dict(policy_snapshot or {}),
    )
    return ctx.to_dict()


def resource_scope_for_security_context(
    security_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Project the request context into a stable resource scope."""
    ctx = dict(security_context or {})
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

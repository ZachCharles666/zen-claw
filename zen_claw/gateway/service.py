"""Single control-plane issuer for zero-trust gateway envelopes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from zen_claw.config.schema import ToolPolicyConfig
from zen_claw.security_context import (
    is_trusted_local_surface,
    issue_gateway_envelope,
    normalize_workspace_id,
    security_policy_snapshot,
)


class GatewayControlPlane:
    """Centralize subject defaults, policy snapshots, and envelope issuance."""

    def __init__(
        self,
        *,
        workspace_path: str | Path,
        tool_policy: ToolPolicyConfig,
        restrict_to_workspace: bool = False,
        profile_allowed_tools: list[str] | None = None,
        profile_denied_tools: list[str] | None = None,
        agent_id: str = "default",
        dev_profile: bool = False,
        trusted_local_only: bool = False,
    ) -> None:
        self.workspace_path = str(workspace_path)
        self.tool_policy = tool_policy
        self.restrict_to_workspace = bool(restrict_to_workspace)
        self.profile_allowed_tools = list(profile_allowed_tools or [])
        self.profile_denied_tools = list(profile_denied_tools or [])
        self.agent_id = str(agent_id or "default").strip().lower() or "default"
        self.dev_profile = bool(dev_profile)
        self.trusted_local_only = bool(trusted_local_only)

    @property
    def local_identity_channels(self) -> set[str]:
        return {
            str(v).strip().lower()
            for v in (self.tool_policy.local_identity_channels or [])
            if str(v).strip()
        }

    def is_local_identity_channel(self, channel_name: str) -> bool:
        return is_trusted_local_surface(channel_name, self.local_identity_channels)

    def build_policy_snapshot(self, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        return security_policy_snapshot(
            production_hardening=bool(self.tool_policy.production_hardening),
            legacy_compat=bool(self.tool_policy.legacy_compat),
            restrict_to_workspace=bool(self.restrict_to_workspace),
            profile_allowed_tools=list(self.profile_allowed_tools),
            profile_denied_tools=list(self.profile_denied_tools),
            extra={
                "trusted_local_only": bool(self.trusted_local_only),
                "dev_profile": bool(self.dev_profile),
                **dict(extra or {}),
            },
        )

    def issue_envelope(
        self,
        *,
        trace_id: str,
        channel: str,
        sender_id: str,
        chat_id: str,
        tenant_id: str = "default",
        workspace_id: str = "",
        agent_profile: str = "",
        role: str = "",
        origin_surface: str = "",
        channel_role: str = "",
        subject_type: str = "agent",
        capability_grants: list[str] | tuple[str, ...] | None = None,
        policy_snapshot_extra: dict[str, Any] | None = None,
        trust_level: str | None = None,
        trust_tier: str | None = None,
    ) -> dict[str, Any]:
        normalized_channel = str(channel or "").strip().lower()
        local_identity = self.is_local_identity_channel(normalized_channel)
        effective_trust = str(
            trust_level
            or ("trusted_local" if local_identity else "remote_untrusted")
        ).strip().lower()
        effective_tier = str(trust_tier or effective_trust).strip().lower()
        return issue_gateway_envelope(
            trace_id=str(trace_id or "").strip(),
            channel=normalized_channel,
            sender_id=str(sender_id or "").strip(),
            chat_id=str(chat_id or "").strip(),
            tenant_id=str(tenant_id or "default").strip().lower() or "default",
            workspace_id=str(
                workspace_id or normalize_workspace_id(self.workspace_path) or ""
            ).strip(),
            agent_profile=str(agent_profile or self.agent_id or "").strip().lower(),
            role=str(role or "").strip().lower(),
            trust_level=effective_trust,
            origin_surface=str(origin_surface or normalized_channel or "").strip().lower(),
            channel_role=str(channel_role or "").strip().lower(),
            workspace_path=self.workspace_path,
            dev_profile=bool(self.dev_profile),
            trusted_local_only=bool(self.trusted_local_only),
            policy_snapshot=self.build_policy_snapshot(extra=policy_snapshot_extra),
            subject_type=str(subject_type or "agent").strip().lower() or "agent",
            trust_tier=effective_tier,
            capability_grants=list(capability_grants or []),
        )

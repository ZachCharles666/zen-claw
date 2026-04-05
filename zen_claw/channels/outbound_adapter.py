"""Outbound isolation adapter for channel writes."""

from __future__ import annotations

import json
from typing import Any

from zen_claw.agent.tools.capability_policy import (
    CapabilityPolicyEvaluator,
    connector_capability_request,
)
from zen_claw.agent.tools.connector_sidecar import ConnectorSidecarClient
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult
from zen_claw.bus.events import OutboundMessage
from zen_claw.config.schema import Config
from zen_claw.gateway import GatewayControlPlane
from zen_claw.security_context import normalize_workspace_id


class ChannelOutboundAdapter:
    """Route external channel writes through the connector sidecar."""

    _DEFAULT_LOCAL_CHANNELS = frozenset({"cli", "system"})

    def __init__(self, config: Config):
        self.config = config
        connector_cfg = config.tools.network.connector
        self._sidecar = ConnectorSidecarClient(
            proxy_url=connector_cfg.proxy_url,
            approval_mode=connector_cfg.sidecar_approval_mode,
            approval_token=connector_cfg.sidecar_approval_token.get_secret_value(),
        )
        self._trusted_local_channels = {
            str(ch).strip().lower()
            for ch in (
                list(config.tools.policy.local_identity_channels or [])
                + list(self._DEFAULT_LOCAL_CHANNELS)
            )
            if str(ch).strip()
        }
        self._gateway = GatewayControlPlane(
            workspace_path=config.workspace_path,
            tool_policy=config.tools.policy,
            agent_id="channel_outbound_adapter",
        )

    @property
    def trusted_local_channels(self) -> set[str]:
        return set(self._trusted_local_channels)

    @property
    def sidecar_target(self) -> str:
        return str(self._sidecar.proxy_url or "")

    def outbound_mode_for_channel(self, channel_name: str) -> str:
        channel = str(channel_name or "").strip().lower()
        if channel in self._trusted_local_channels:
            return "local_only"
        if self._sidecar.proxy_url:
            return "isolated_external"
        return "blocked_no_sidecar"

    def _identity_context(self, msg: OutboundMessage, trace_id: str) -> dict[str, Any]:
        meta = msg.metadata if isinstance(msg.metadata, dict) else {}
        agent_profile = str(
            meta.get("agent_profile")
            or meta.get("profile_id")
            or meta.get("routed_agent_id")
            or "default"
        ).strip().lower()
        workspace_id = str(
            meta.get("workspace_id")
            or meta.get("workspace")
            or normalize_workspace_id(self.config.workspace_path)
            or "default"
        ).strip()
        return self._gateway.issue_envelope(
            trace_id=trace_id,
            channel=str(meta.get("origin_channel") or "system").strip().lower() or "system",
            sender_id=str(meta.get("sender_id") or meta.get("route_user_id") or "").strip(),
            chat_id=str(meta.get("origin_chat_id") or msg.chat_id or "").strip(),
            tenant_id=str(meta.get("tenant_id") or "default").strip().lower(),
            workspace_id=workspace_id,
            agent_profile=agent_profile,
            role=str(meta.get("role") or meta.get("channel_role") or "operator").strip().lower(),
            origin_surface=str(meta.get("origin_surface") or msg.channel or "system").strip().lower(),
            channel_role=str(meta.get("channel_role") or "").strip().lower(),
            subject_type="channel",
            capability_grants=["connector.file.upload", "connector.message.send"],
            trust_level=str(meta.get("trust_level") or "trusted_local").strip().lower(),
            trust_tier=str(meta.get("trust_tier") or meta.get("trust_level") or "trusted_local")
            .strip()
            .lower(),
            policy_snapshot_extra={
                "production_hardening": bool(self.config.tools.policy.production_hardening),
                "legacy_compat": bool(self.config.tools.policy.legacy_compat),
                "policy_snapshot_hash": str(
                    meta.get("policy_snapshot_hash")
                    or ((meta.get("policy_snapshot") or {}).get("policy_snapshot_hash"))
                    or ""
                ),
            },
        )

    def _validate_policy(
        self,
        *,
        channel_name: str,
        action: str,
        target_resource: str,
        identity: dict[str, Any],
    ) -> ToolResult | None:
        evaluator = CapabilityPolicyEvaluator(self.config.tools.policy, identity)
        request = connector_capability_request(
            capability=action,
            connector_name=channel_name,
            target_resource=target_resource,
            tenant_id=str(identity.get("tenant_id") or "").strip().lower(),
            workspace_id=str(identity.get("workspace_id") or "").strip(),
        )
        decision = evaluator.evaluate(request)
        if decision.allowed:
            return None
        code = str(decision.error_code or "")
        if code == "resource_scope_connector_unconfigured":
            code = "channel_outbound_unconfigured"
        elif code == "resource_scope_connector_name":
            code = "channel_outbound_connector_name"
        elif code == "resource_scope_connector_action":
            code = "channel_outbound_connector_action"
        elif code == "resource_scope_connector_target":
            code = "channel_outbound_connector_target"
        elif code == "resource_scope_connector_tenant":
            code = "channel_outbound_connector_tenant"
        elif code == "resource_scope_connector_workspace":
            code = "channel_outbound_connector_workspace"
        return ToolResult.failure(
            ToolErrorKind.PERMISSION,
            decision.reason,
            code=code,
            capability=str(decision.capability or ""),
            resource_scope=dict(decision.resource_scope or {}),
            matched_scope=dict(decision.matched_scope or {}),
            policy_snapshot=dict(decision.policy_snapshot or {}),
            identity={
                "channel": str(identity.get("channel") or ""),
                "sender_id": str(identity.get("sender_id") or ""),
                "chat_id": str(identity.get("chat_id") or ""),
                "tenant_id": str(identity.get("tenant_id") or ""),
                "workspace_id": str(identity.get("workspace_id") or ""),
                "agent_profile": str(identity.get("agent_profile") or ""),
                "role": str(identity.get("role") or ""),
            },
            sidecar_target=self.sidecar_target,
        )

    async def dispatch(self, msg: OutboundMessage, *, trace_id: str) -> list[tuple[str, ToolResult]]:
        channel = str(msg.channel or "").strip().lower()
        if channel in self._trusted_local_channels:
            return []
        if not self._sidecar.proxy_url:
            return [
                (
                    "connector.message.send",
                    ToolResult.failure(
                        ToolErrorKind.RUNTIME,
                        "external channel outbound requires connector sidecar configuration",
                        code="channel_outbound_sidecar_not_configured",
                    ),
                )
            ]
        identity = self._identity_context(msg, trace_id)
        results: list[tuple[str, ToolResult]] = []
        if msg.media:
            upload_action = "connector.file.upload"
            upload_target = f"channel.{channel}.file"
            denied = self._validate_policy(
                channel_name=channel,
                action=upload_action,
                target_resource=upload_target,
                identity=identity,
            )
            if denied is not None:
                return [(upload_action, denied)]
            upload_payload = self._sidecar.build_request_payload(
                target_url=f"channel://{channel}/{msg.chat_id}/attachments",
                method="POST",
                headers={"Content-Type": "application/json"},
                body=json.dumps(
                    {
                        "channel": msg.channel,
                        "chat_id": msg.chat_id,
                        "reply_to": msg.reply_to,
                        "media": list(msg.media or []),
                        "metadata": dict(msg.metadata or {}),
                    },
                    ensure_ascii=False,
                ),
                connector_name=channel,
                action=upload_action,
                target_resource=upload_target,
                security_context=identity,
            )
            upload_result = await self._sidecar.execute_request(
                request_payload=upload_payload,
                trace_id=trace_id,
            )
            results.append((upload_action, upload_result))
            if not upload_result.ok:
                return results

        if str(msg.content or "").strip() or msg.reply_to:
            send_action = "connector.message.send"
            send_target = f"channel.{channel}.message"
            denied = self._validate_policy(
                channel_name=channel,
                action=send_action,
                target_resource=send_target,
                identity=identity,
            )
            if denied is not None:
                results.append((send_action, denied))
                return results
            send_payload = self._sidecar.build_request_payload(
                target_url=f"channel://{channel}/{msg.chat_id}",
                method="POST",
                headers={"Content-Type": "application/json"},
                body=json.dumps(
                    {
                        "channel": msg.channel,
                        "chat_id": msg.chat_id,
                        "content": msg.content,
                        "reply_to": msg.reply_to,
                        "media": list(msg.media or []),
                        "metadata": dict(msg.metadata or {}),
                    },
                    ensure_ascii=False,
                ),
                connector_name=channel,
                action=send_action,
                target_resource=send_target,
                security_context=identity,
            )
            send_result = await self._sidecar.execute_request(
                request_payload=send_payload,
                trace_id=trace_id,
            )
            results.append((send_action, send_result))
        return results

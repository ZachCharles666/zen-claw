"""Shared capability policy evaluation for zero-trust resource checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from zen_claw.agent.tools.result import ToolErrorKind, ToolResult


@dataclass
class CapabilityRequest:
    """Normalized capability request."""

    capability: str
    resource_scope: dict[str, Any] = field(default_factory=dict)


@dataclass
class CapabilityPolicyDecision:
    """Policy evaluation result."""

    allowed: bool
    capability: str
    reason: str = ""
    error_code: str = ""
    matched_scope: dict[str, Any] = field(default_factory=dict)
    policy_snapshot: dict[str, Any] = field(default_factory=dict)
    resource_scope: dict[str, Any] = field(default_factory=dict)


class CapabilityPolicyEvaluator:
    """Evaluate resource-scoped capability policy using existing config fields."""

    def __init__(self, policy: Any, identity: dict[str, Any] | None = None):
        self._policy = policy
        self._identity = dict(identity or {})
        self._policy_snapshot = dict(self._identity.get("policy_snapshot") or {})
        if not self._policy_snapshot:
            self._policy_snapshot = {
                "production_hardening": bool(getattr(policy, "production_hardening", False))
                if policy is not None
                else False,
                "legacy_compat": bool(getattr(policy, "legacy_compat", False))
                if policy is not None
                else False,
            }

    def evaluate(self, request: CapabilityRequest | None) -> CapabilityPolicyDecision:
        if request is None:
            return CapabilityPolicyDecision(allowed=True, capability="")
        if self._policy is None:
            return self._allow(request.capability, request.resource_scope)
        if bool(getattr(self._policy, "legacy_compat", False)):
            return self._allow(
                request.capability,
                request.resource_scope,
                matched_scope={"legacy_compat": True},
            )

        capability = str(request.capability or "").strip().lower()
        scope = dict(request.resource_scope or {})
        if capability == "filesystem.access":
            return self._evaluate_filesystem(scope)
        if capability == "exec.run":
            return self._evaluate_exec(scope)
        if capability.startswith("sessions."):
            return self._evaluate_sessions(capability, scope)
        if capability in {"network.fetch", "network.search", "network.browser"}:
            return self._evaluate_network(capability, scope)
        if capability.startswith("connector."):
            return self._evaluate_connector(capability, scope)
        if capability == "message.send":
            return self._evaluate_message(scope)
        if capability.startswith("knowledge."):
            return self._evaluate_knowledge(capability, scope)
        return self._allow(capability, scope)

    def denial_result(
        self,
        decision: CapabilityPolicyDecision,
        *,
        identity: dict[str, Any],
        dev_profile: bool = False,
        sidecar_target: str = "",
        skill_names: list[str] | None = None,
        skill_permissions_mode: str = "off",
    ) -> ToolResult:
        return ToolResult.failure(
            ToolErrorKind.PERMISSION,
            decision.reason,
            code=decision.error_code,
            policy_scope="resource",
            identity=dict(identity or {}),
            policy_snapshot=dict(decision.policy_snapshot or self._policy_snapshot),
            resource_scope=dict(decision.resource_scope or {}),
            matched_scope=dict(decision.matched_scope or {}),
            capability=str(decision.capability or ""),
            dev_profile=bool(dev_profile),
            sidecar_target=str(sidecar_target or ""),
            skill_names=skill_names,
            skill_permissions_mode=skill_permissions_mode,
        )

    def _allow(
        self,
        capability: str,
        resource_scope: dict[str, Any],
        *,
        matched_scope: dict[str, Any] | None = None,
    ) -> CapabilityPolicyDecision:
        return CapabilityPolicyDecision(
            allowed=True,
            capability=capability,
            matched_scope=dict(matched_scope or {}),
            policy_snapshot=dict(self._policy_snapshot),
            resource_scope=dict(resource_scope or {}),
        )

    def _deny(
        self,
        capability: str,
        resource_scope: dict[str, Any],
        *,
        reason: str,
        error_code: str,
        matched_scope: dict[str, Any] | None = None,
    ) -> CapabilityPolicyDecision:
        return CapabilityPolicyDecision(
            allowed=False,
            capability=capability,
            reason=reason,
            error_code=error_code,
            matched_scope=dict(matched_scope or {}),
            policy_snapshot=dict(self._policy_snapshot),
            resource_scope=dict(resource_scope or {}),
        )

    def _evaluate_filesystem(self, scope: dict[str, Any]) -> CapabilityPolicyDecision:
        path_value = str(scope.get("filesystem_path") or "").strip()
        workspace_path = str(scope.get("workspace_path") or self._identity.get("workspace_path") or "").strip()
        if not path_value or not workspace_path:
            return self._allow("filesystem.access", scope)
        try:
            resolved = Path(path_value).expanduser().resolve()
            workspace = Path(workspace_path).expanduser().resolve()
        except Exception:
            return self._deny(
                "filesystem.access",
                scope,
                reason="filesystem path could not be resolved",
                error_code="resource_scope_filesystem_invalid",
            )
        normalized_scope = {
            **scope,
            "filesystem_path": str(resolved),
            "workspace_path": str(workspace),
        }
        hardening = bool(getattr(self._policy, "production_hardening", False))
        if workspace not in resolved.parents and resolved != workspace:
            return self._deny(
                "filesystem.access",
                normalized_scope,
                reason="filesystem path is outside workspace boundary",
                error_code="resource_scope_filesystem_workspace",
                matched_scope={"workspace": str(workspace)},
            )
        allowed_subpaths = list(getattr(self._policy, "filesystem_allowed_subpaths", []) or [])
        if not allowed_subpaths:
            if hardening:
                return self._deny(
                    "filesystem.access",
                    normalized_scope,
                    reason="filesystem access is disabled until subpaths are explicitly allowlisted",
                    error_code="resource_scope_filesystem_unconfigured",
                )
            return self._allow("filesystem.access", normalized_scope)
        for raw in allowed_subpaths:
            candidate = (workspace / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
            if candidate == resolved or candidate in resolved.parents:
                return self._allow(
                    "filesystem.access",
                    normalized_scope,
                    matched_scope={"filesystem_allowed_subpath": str(candidate)},
                )
        return self._deny(
            "filesystem.access",
            normalized_scope,
            reason="filesystem path is not within an allowlisted subpath",
            error_code="resource_scope_filesystem_subpath",
            matched_scope={"filesystem_allowed_subpaths": allowed_subpaths},
        )

    def _evaluate_exec(self, scope: dict[str, Any]) -> CapabilityPolicyDecision:
        workspace_path = str(scope.get("workspace_path") or self._identity.get("workspace_path") or "").strip()
        working_dir = str(scope.get("working_dir") or workspace_path or "").strip()
        hardening = bool(getattr(self._policy, "production_hardening", False))
        normalized_scope = dict(scope)
        if working_dir:
            try:
                resolved_wd = Path(working_dir).expanduser().resolve()
                normalized_scope["working_dir"] = str(resolved_wd)
                if workspace_path:
                    workspace = Path(workspace_path).expanduser().resolve()
                    normalized_scope["workspace_path"] = str(workspace)
                    if workspace not in resolved_wd.parents and resolved_wd != workspace:
                        return self._deny(
                            "exec.run",
                            normalized_scope,
                            reason="exec working directory is outside workspace boundary",
                            error_code="resource_scope_exec_workspace",
                            matched_scope={"workspace": str(workspace)},
                        )
                allowed_dirs = list(getattr(self._policy, "exec_allowed_working_dirs", []) or [])
                if allowed_dirs:
                    for raw in allowed_dirs:
                        candidate = (
                            (Path(workspace_path) / raw).resolve()
                            if workspace_path and not Path(raw).is_absolute()
                            else Path(raw).resolve()
                        )
                        if candidate == resolved_wd or candidate in resolved_wd.parents:
                            break
                    else:
                        return self._deny(
                            "exec.run",
                            normalized_scope,
                            reason="exec working directory is not allowlisted",
                            error_code="resource_scope_exec_workdir",
                            matched_scope={"exec_allowed_working_dirs": allowed_dirs},
                        )
                elif hardening:
                    return self._deny(
                        "exec.run",
                        normalized_scope,
                        reason="exec is disabled until working directories are explicitly allowlisted",
                        error_code="resource_scope_exec_workdir_unconfigured",
                    )
            except Exception:
                return self._deny(
                    "exec.run",
                    normalized_scope,
                    reason="exec working directory could not be resolved",
                    error_code="resource_scope_exec_invalid",
                )
        allowed_prefixes = list(getattr(self._policy, "exec_allowed_command_prefixes", []) or [])
        command = str(scope.get("command") or "").strip()
        normalized_scope["command"] = command[:80]
        if allowed_prefixes:
            normalized_command = " ".join(command.lower().split())
            if not any(
                normalized_command == prefix or normalized_command.startswith(prefix + " ")
                for prefix in allowed_prefixes
            ):
                return self._deny(
                    "exec.run",
                    normalized_scope,
                    reason="exec command is not allowlisted",
                    error_code="resource_scope_exec_prefix",
                    matched_scope={"exec_allowed_command_prefixes": allowed_prefixes},
                )
        elif hardening:
            return self._deny(
                "exec.run",
                normalized_scope,
                reason="exec is disabled until command prefixes are explicitly allowlisted",
                error_code="resource_scope_exec_prefix_unconfigured",
            )
        return self._allow("exec.run", normalized_scope)

    def _evaluate_sessions(
        self,
        capability: str,
        scope: dict[str, Any],
    ) -> CapabilityPolicyDecision:
        workspace_path = str(scope.get("workspace_path") or self._identity.get("workspace_path") or "").strip()
        hardening = bool(getattr(self._policy, "production_hardening", False))
        normalized_scope = dict(scope)
        session_op = str(scope.get("session_op") or capability.removeprefix("sessions.")).strip().lower()
        session_owner = dict(scope.get("session_owner") or {})
        normalized_scope["session_op"] = session_op

        if session_op == "spawn":
            exec_scope = {
                **scope,
                "workspace_path": workspace_path,
            }
            exec_decision = self._evaluate_exec(exec_scope)
            if not exec_decision.allowed:
                return CapabilityPolicyDecision(
                    allowed=False,
                    capability=capability,
                    reason=exec_decision.reason,
                    error_code=exec_decision.error_code.replace("exec.", "sessions."),
                    matched_scope=dict(exec_decision.matched_scope or {}),
                    policy_snapshot=dict(exec_decision.policy_snapshot or self._policy_snapshot),
                    resource_scope=dict(exec_decision.resource_scope or normalized_scope),
                )
            return self._allow(capability, dict(exec_decision.resource_scope or normalized_scope))

        allowed_ops = list(getattr(self._policy, "sessions_allowed_session_ops", []) or [])
        if allowed_ops:
            if session_op not in allowed_ops:
                return self._deny(
                    capability,
                    normalized_scope,
                    reason=f"session op '{session_op or '-'}' is not allowlisted",
                    error_code="resource_scope_sessions_op",
                    matched_scope={"sessions_allowed_session_ops": allowed_ops},
                )
        elif hardening:
            return self._deny(
                capability,
                normalized_scope,
                reason="session control is disabled until session ops are explicitly allowlisted",
                error_code="resource_scope_sessions_op_unconfigured",
            )

        current_tenant = str(scope.get("tenant_id") or self._identity.get("tenant_id") or "").strip().lower()
        current_workspace = str(scope.get("workspace_id") or self._identity.get("workspace_id") or "").strip()
        owner_tenant = str(session_owner.get("tenant_id") or "").strip().lower()
        owner_workspace = str(session_owner.get("workspace_id") or "").strip()
        if owner_tenant and current_tenant and owner_tenant != current_tenant:
            return self._deny(
                capability,
                normalized_scope,
                reason="session belongs to a different tenant",
                error_code="resource_scope_sessions_cross_tenant",
                matched_scope={"current_tenant": current_tenant, "session_owner_tenant": owner_tenant},
            )
        if owner_workspace and current_workspace and owner_workspace != current_workspace:
            return self._deny(
                capability,
                normalized_scope,
                reason="session belongs to a different workspace",
                error_code="resource_scope_sessions_cross_workspace",
                matched_scope={
                    "current_workspace": current_workspace,
                    "session_owner_workspace": owner_workspace,
                },
            )
        return self._allow(
            capability,
            normalized_scope,
            matched_scope={"sessions_allowed_session_ops": allowed_ops},
        )

    def _evaluate_network(
        self,
        capability: str,
        scope: dict[str, Any],
    ) -> CapabilityPolicyDecision:
        if capability == "network.fetch":
            allow_attr = "fetch_allowed_domains"
        elif capability == "network.search":
            allow_attr = "search_allowed_domains"
        else:
            allow_attr = "browser_allowed_domains"
        allowed_domains = list(getattr(self._policy, allow_attr, []) or [])
        hardening = bool(getattr(self._policy, "production_hardening", False))
        host = str(scope.get("domain") or "").strip().lower()
        url_value = str(scope.get("url") or "").strip()
        if not host and url_value:
            host = str(urlparse(url_value).hostname or "").strip().lower()
        normalized_scope = {**scope, "domain": host}
        if allowed_domains:
            if not host:
                return self._deny(
                    capability,
                    normalized_scope,
                    reason="network action requires an explicit allowlisted domain",
                    error_code="resource_scope_domain_missing",
                    matched_scope={"allowed_domains": allowed_domains},
                )
            if not any(host == domain or host.endswith("." + domain) for domain in allowed_domains):
                return self._deny(
                    capability,
                    normalized_scope,
                    reason=f"domain '{host}' is not allowlisted",
                    error_code="resource_scope_domain_denied",
                    matched_scope={"allowed_domains": allowed_domains},
                )
            return self._allow(
                capability,
                normalized_scope,
                matched_scope={"allowed_domains": allowed_domains},
            )
        if hardening:
            tool_label = capability.replace("network.", "web_") if capability != "network.browser" else "browser"
            return self._deny(
                capability,
                normalized_scope,
                reason=f"{tool_label} is disabled until domains are explicitly allowlisted",
                error_code="resource_scope_domain_unconfigured",
            )
        return self._allow(capability, normalized_scope)

    def _evaluate_connector(
        self,
        capability: str,
        scope: dict[str, Any],
    ) -> CapabilityPolicyDecision:
        connector_name = str(scope.get("connector_name") or "").strip().lower()
        action = str(scope.get("action") or capability).strip().lower()
        target_resource = str(scope.get("target_resource") or "").strip().lower()
        tenant_id = str(scope.get("tenant_id") or self._identity.get("tenant_id") or "").strip().lower()
        workspace_id = str(scope.get("workspace_id") or self._identity.get("workspace_id") or "").strip()
        current_tenant = str(scope.get("current_tenant_id") or self._identity.get("tenant_id") or "").strip().lower()
        current_workspace = str(scope.get("current_workspace_id") or self._identity.get("workspace_id") or "").strip()
        normalized_scope = {
            **scope,
            "connector_name": connector_name,
            "action": action,
            "target_resource": target_resource,
            "tenant_id": tenant_id,
            "workspace_id": workspace_id,
        }
        if not connector_name or not action or not target_resource:
            return self._deny(
                capability,
                normalized_scope,
                reason="connector write requires connector_name, action, and target_resource",
                error_code="resource_scope_connector_metadata_missing",
            )
        if tenant_id and current_tenant and tenant_id != current_tenant:
            return self._deny(
                capability,
                normalized_scope,
                reason="connector write cannot target a different tenant",
                error_code="resource_scope_connector_cross_tenant",
                matched_scope={"current_tenant": current_tenant},
            )
        if workspace_id and current_workspace and workspace_id != current_workspace:
            return self._deny(
                capability,
                normalized_scope,
                reason="connector write cannot target a different workspace",
                error_code="resource_scope_connector_cross_workspace",
                matched_scope={"current_workspace": current_workspace},
            )
        allowed_names = list(getattr(self._policy, "connector_allowed_names", []) or [])
        allowed_actions = list(getattr(self._policy, "connector_allowed_actions", []) or [])
        allowed_targets = list(getattr(self._policy, "connector_allowed_target_resources", []) or [])
        allowed_tenants = list(getattr(self._policy, "connector_allowed_tenants", []) or [])
        allowed_workspaces = list(getattr(self._policy, "connector_allowed_workspaces", []) or [])
        if not any([allowed_names, allowed_actions, allowed_targets, allowed_tenants, allowed_workspaces]):
            if bool(getattr(self._policy, "production_hardening", False)):
                return self._deny(
                    capability,
                    normalized_scope,
                    reason="connector write tools are disabled until connector policy is explicitly configured",
                    error_code="resource_scope_connector_unconfigured",
                )
            return self._allow(capability, normalized_scope)
        if allowed_names and connector_name not in allowed_names:
            return self._deny(
                capability,
                normalized_scope,
                reason=f"connector '{connector_name}' is not allowlisted",
                error_code="resource_scope_connector_name",
                matched_scope={"allowed_connectors": allowed_names},
            )
        if allowed_actions and action not in allowed_actions:
            return self._deny(
                capability,
                normalized_scope,
                reason=f"connector action '{action}' is not allowlisted",
                error_code="resource_scope_connector_action",
                matched_scope={"allowed_actions": allowed_actions},
            )
        if allowed_targets and not any(
            target_resource == target or target_resource.startswith(target + ".")
            for target in allowed_targets
        ):
            return self._deny(
                capability,
                normalized_scope,
                reason=f"connector target_resource '{target_resource}' is not allowlisted",
                error_code="resource_scope_connector_target",
                matched_scope={"allowed_target_resources": allowed_targets},
            )
        if allowed_tenants and tenant_id not in allowed_tenants:
            return self._deny(
                capability,
                normalized_scope,
                reason=f"connector tenant '{tenant_id}' is not allowlisted",
                error_code="resource_scope_connector_tenant",
                matched_scope={"allowed_tenants": allowed_tenants},
            )
        if allowed_workspaces and workspace_id not in allowed_workspaces:
            return self._deny(
                capability,
                normalized_scope,
                reason=f"connector workspace '{workspace_id}' is not allowlisted",
                error_code="resource_scope_connector_workspace",
                matched_scope={"allowed_workspaces": allowed_workspaces},
            )
        return self._allow(
            capability,
            normalized_scope,
            matched_scope={
                "allowed_connectors": allowed_names,
                "allowed_actions": allowed_actions,
                "allowed_target_resources": allowed_targets,
            },
        )

    def _evaluate_message(self, scope: dict[str, Any]) -> CapabilityPolicyDecision:
        allowed_channels = list(getattr(self._policy, "message_allowed_channels", []) or [])
        target_channel = str(scope.get("channel") or self._identity.get("channel") or "").strip().lower()
        normalized_scope = {**scope, "channel": target_channel}
        if allowed_channels:
            if target_channel not in allowed_channels:
                return self._deny(
                    "message.send",
                    normalized_scope,
                    reason=f"message target channel '{target_channel or '-'}' is not allowlisted",
                    error_code="resource_scope_message_channel",
                    matched_scope={"allowed_channels": allowed_channels},
                )
            return self._allow(
                "message.send",
                normalized_scope,
                matched_scope={"allowed_channels": allowed_channels},
            )
        if bool(getattr(self._policy, "production_hardening", False)):
            return self._deny(
                "message.send",
                normalized_scope,
                reason="message tool is disabled until target channels are explicitly allowlisted",
                error_code="resource_scope_message_unconfigured",
            )
        return self._allow("message.send", normalized_scope)

    def _evaluate_knowledge(
        self,
        capability: str,
        scope: dict[str, Any],
    ) -> CapabilityPolicyDecision:
        allowed_tenants = list(getattr(self._policy, "knowledge_allowed_tenants", []) or [])
        allowed_notebooks = list(getattr(self._policy, "knowledge_allowed_notebooks", []) or [])
        hardening = bool(getattr(self._policy, "production_hardening", False))
        tenant_id = str(scope.get("tenant_id") or self._identity.get("tenant_id") or "default").strip().lower()
        notebook_id = str(scope.get("notebook_id") or "default").strip().lower()
        normalized_scope = {**scope, "tenant_id": tenant_id, "notebook_id": notebook_id}
        if not allowed_tenants and not allowed_notebooks and hardening:
            return self._deny(
                capability,
                normalized_scope,
                reason="knowledge access is disabled until tenants or notebooks are explicitly allowlisted",
                error_code="resource_scope_knowledge_unconfigured",
            )
        if allowed_tenants and tenant_id not in allowed_tenants:
            return self._deny(
                capability,
                normalized_scope,
                reason=f"knowledge tenant '{tenant_id}' is not allowlisted",
                error_code="resource_scope_knowledge_tenant",
                matched_scope={"allowed_tenants": allowed_tenants},
            )
        if allowed_notebooks and notebook_id not in allowed_notebooks:
            return self._deny(
                capability,
                normalized_scope,
                reason=f"knowledge notebook '{notebook_id}' is not allowlisted",
                error_code="resource_scope_knowledge_notebook",
                matched_scope={"allowed_notebooks": allowed_notebooks},
            )
        return self._allow(
            capability,
            normalized_scope,
            matched_scope={
                "allowed_tenants": allowed_tenants,
                "allowed_notebooks": allowed_notebooks,
            },
        )


def capability_request_from_tool(
    tool_name: str,
    params: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> CapabilityRequest | None:
    """Map a tool invocation to a capability request."""

    tool = str(tool_name or "").strip().lower()
    ctx = dict(context or {})
    workspace_path = str(ctx.get("workspace_path") or "").strip()

    if tool in {"read_file", "write_file", "edit_file", "list_dir"}:
        return CapabilityRequest(
            capability="filesystem.access",
            resource_scope={
                "filesystem_path": str(params.get("path") or "").strip(),
                "workspace_path": workspace_path,
            },
        )

    if tool == "exec":
        return CapabilityRequest(
            capability="exec.run",
            resource_scope={
                "working_dir": str(
                    params.get("working_dir")
                    or params.get("cwd")
                    or params.get("path")
                    or workspace_path
                    or ""
                ).strip(),
                "command": str(
                    params.get("command") or params.get("input") or params.get("signal") or ""
                ).strip(),
                "workspace_path": workspace_path,
            },
        )

    if tool.startswith("sessions_"):
        session_id = str(params.get("session_id") or "").strip()
        capability = "sessions." + tool.removeprefix("sessions_")
        return CapabilityRequest(
            capability=capability,
            resource_scope={
                "working_dir": str(
                    params.get("working_dir")
                    or params.get("cwd")
                    or workspace_path
                    or ""
                ).strip(),
                "command": str(params.get("command") or "").strip(),
                "workspace_path": workspace_path,
                "tenant_id": str(ctx.get("tenant_id") or "").strip().lower(),
                "workspace_id": str(ctx.get("workspace_id") or "").strip(),
                "session_id": session_id,
                "session_op": tool.removeprefix("sessions_"),
                "session_owner": dict(params.get("__session_owner") or {}),
            },
        )

    if tool == "web_fetch":
        return CapabilityRequest(
            capability="network.fetch",
            resource_scope={"url": str(params.get("url") or "").strip()},
        )

    if tool == "web_search":
        return CapabilityRequest(
            capability="network.search",
            resource_scope={"url": str(params.get("url") or "").strip()},
        )

    if tool.startswith("browser_"):
        return CapabilityRequest(
            capability="network.browser",
            resource_scope={"url": str(params.get("url") or "").strip()},
        )

    if tool in {"social_platform_post", "social_platform_like"}:
        return connector_capability_request(
            capability=str(params.get("action") or "connector.record.create").strip().lower(),
            connector_name=str(params.get("connector_name") or "").strip().lower(),
            target_resource=str(params.get("target_resource") or "").strip().lower(),
            tenant_id=str(params.get("tenant_id") or ctx.get("tenant_id") or "").strip().lower(),
            workspace_id=str(params.get("workspace_id") or ctx.get("workspace_id") or "").strip(),
            current_tenant_id=str(ctx.get("tenant_id") or "").strip().lower(),
            current_workspace_id=str(ctx.get("workspace_id") or "").strip(),
        )

    if tool == "message":
        return CapabilityRequest(
            capability="message.send",
            resource_scope={
                "channel": str(params.get("channel") or ctx.get("channel") or "").strip().lower(),
            },
        )

    if tool.startswith("knowledge_"):
        return CapabilityRequest(
            capability="knowledge.read",
            resource_scope={
                "tenant_id": str(params.get("tenant_id") or ctx.get("tenant_id") or "default").strip().lower(),
                "notebook_id": str(params.get("notebook_id") or "default").strip().lower(),
            },
        )

    return None


def connector_capability_request(
    *,
    capability: str,
    connector_name: str,
    target_resource: str,
    tenant_id: str = "",
    workspace_id: str = "",
    current_tenant_id: str = "",
    current_workspace_id: str = "",
) -> CapabilityRequest:
    """Build a connector capability request."""

    normalized_capability = str(capability or "").strip().lower()
    return CapabilityRequest(
        capability=normalized_capability,
        resource_scope={
            "connector_name": str(connector_name or "").strip().lower(),
            "action": normalized_capability,
            "target_resource": str(target_resource or "").strip().lower(),
            "tenant_id": str(tenant_id or "").strip().lower(),
            "workspace_id": str(workspace_id or "").strip(),
            "current_tenant_id": str(current_tenant_id or "").strip().lower(),
            "current_workspace_id": str(current_workspace_id or "").strip(),
        },
    )

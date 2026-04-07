from typing import Any

from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.capability_policy import (
    CapabilityPolicyEvaluator,
    CapabilityRequest,
    connector_capability_request,
)
from zen_claw.agent.tools.policy import PolicyReason, ToolPolicyEngine
from zen_claw.agent.tools.registry import ToolRegistry
from zen_claw.agent.tools.result import ToolErrorKind
from zen_claw.config.loader import convert_keys
from zen_claw.config.schema import Config


class _DangerTool(Tool):
    @property
    def name(self) -> str:
        return "danger"

    @property
    def description(self) -> str:
        return "danger"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


def test_policy_engine_deny_precedence_over_allow() -> None:
    engine = ToolPolicyEngine(default_deny_tools=set())
    engine.set_scope("agent", allow={"*"})
    engine.set_scope("session", deny={"exec"})

    denied = engine.evaluate("exec")
    assert denied.allowed is False
    assert denied.code == "tool_policy_denied"
    assert denied.scope == "session"


def test_policy_engine_allowlist_mode_blocks_non_allowlisted_tool() -> None:
    engine = ToolPolicyEngine(default_deny_tools=set())
    engine.set_scope("session", allow={"read_file"})

    denied = engine.evaluate("web_fetch")
    assert denied.allowed is False
    assert denied.code == "tool_policy_not_allowlisted"

    allowed = engine.evaluate("read_file")
    assert allowed.allowed is True


def test_policy_engine_default_deny_for_sensitive_tools() -> None:
    engine = ToolPolicyEngine(default_deny_tools={"exec", "spawn"})
    denied = engine.evaluate("exec")
    assert denied.allowed is False
    assert denied.code == "tool_policy_default_deny"

    allowed = engine.evaluate("read_file")
    assert allowed.allowed is True


def test_policy_engine_normalizes_case_and_whitespace() -> None:
    engine = ToolPolicyEngine(default_deny_tools={" Exec "})
    engine.set_scope("session", deny={" Spawn "})

    denied_default = engine.evaluate("EXEC")
    assert denied_default.allowed is False
    assert denied_default.code == "tool_policy_default_deny"

    denied_scoped = engine.evaluate("spawn")
    assert denied_scoped.allowed is False
    assert denied_scoped.code == "tool_policy_denied"
    assert denied_scoped.scope == "session"

    allowed = engine.evaluate("read_file")
    assert allowed.allowed is True


async def test_registry_blocks_tool_when_policy_denies() -> None:
    engine = ToolPolicyEngine(default_deny_tools={"danger"})
    reg = ToolRegistry(policy=engine)
    reg.register(_DangerTool())

    result = await reg.execute("danger", {})
    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.PERMISSION
    assert result.error.code == "tool_policy_default_deny"
    assert result.meta.get("policy_scope", "") == ""


async def test_registry_policy_scope_is_attached_on_scoped_deny() -> None:
    engine = ToolPolicyEngine(default_deny_tools=set())
    engine.set_scope("session", deny={"danger"})
    reg = ToolRegistry(policy=engine)
    reg.register(_DangerTool())

    result = await reg.execute("danger", {})
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "tool_policy_denied"
    assert result.meta.get("policy_scope") == "session"


async def test_registry_blocks_all_tools_when_kill_switch_enabled() -> None:
    reg = ToolRegistry(policy=ToolPolicyEngine(default_deny_tools=set()))
    reg.register(_DangerTool())
    reg.set_kill_switch(True, reason="incident")

    result = await reg.execute("danger", {})
    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.PERMISSION
    assert result.error.code == "tool_kill_switch_enabled"
    assert result.meta.get("policy_scope") == "global_kill_switch"
    assert "incident" in result.error.message


class _MessageTool(_DangerTool):
    @property
    def name(self) -> str:
        return "message"


class _FetchTool(_DangerTool):
    @property
    def name(self) -> str:
        return "web_fetch"


class _ReadFileTool(_DangerTool):
    @property
    def name(self) -> str:
        return "read_file"


class _ConnectorPostTool(_DangerTool):
    @property
    def name(self) -> str:
        return "social_platform_post"


class _KnowledgeTool(_DangerTool):
    @property
    def name(self) -> str:
        return "knowledge_search"


async def test_registry_denies_message_without_explicit_channel_allowlist() -> None:
    cfg = Config()
    reg = ToolRegistry(policy=ToolPolicyEngine(default_deny_tools=set()))
    reg.register(_MessageTool())
    reg.set_security_policy(cfg.tools.policy)
    reg.set_request_context(
        {
            "channel": "cli",
            "sender_id": "user-1",
            "chat_id": "chat-1",
            "tenant_id": "tenant-a",
            "workspace_id": "ws-1",
            "agent_profile": "default",
            "role": "operator",
            "workspace_path": ".",
            "policy_snapshot": {"production_hardening": True},
        }
    )

    denied = await reg.execute("message", {"content": "hello", "channel": "slack"})
    assert denied.ok is False
    assert denied.error is not None
    assert denied.error.kind == ToolErrorKind.PERMISSION
    assert denied.error.code == "resource_scope_message_unconfigured"
    assert denied.meta.get("policy_scope") == "resource"
    assert denied.meta.get("capability") == "message.send"


async def test_registry_denies_high_risk_tool_with_tampered_gateway_envelope() -> None:
    cfg = Config.model_validate(
        convert_keys({"tools": {"policy": {"fetchAllowedDomains": ["example.com"]}}})
    )
    reg = ToolRegistry(policy=ToolPolicyEngine(default_deny_tools=set()))
    reg.register(_FetchTool())
    reg.set_security_policy(cfg.tools.policy)
    reg.set_request_context(
        {
            "trace_id": "trace-fetch",
            "channel": "cli",
            "sender_id": "user-1",
            "chat_id": "chat-1",
            "tenant_id": "tenant-a",
            "workspace_id": "ws-1",
            "agent_profile": "default",
            "role": "operator",
            "workspace_path": ".",
            "policy_snapshot": {"production_hardening": True},
        }
    )
    reg._request_context["gateway_meta"]["request_hash"] = "tampered"

    denied = await reg.execute("web_fetch", {"url": "https://example.com/"})
    assert denied.ok is False
    assert denied.error is not None
    assert denied.error.code == "gateway_envelope_invalid"
    assert denied.meta.get("policy_scope") == "gateway"


async def test_registry_denies_high_risk_tool_without_capability_grant() -> None:
    cfg = Config.model_validate(
        convert_keys({"tools": {"policy": {"fetchAllowedDomains": ["example.com"]}}})
    )
    reg = ToolRegistry(policy=ToolPolicyEngine(default_deny_tools=set()))
    reg.register(_FetchTool())
    reg.set_security_policy(cfg.tools.policy)
    reg.set_request_context(
        {
            "trace_id": "trace-fetch",
            "channel": "cli",
            "sender_id": "user-1",
            "chat_id": "chat-1",
            "tenant_id": "tenant-a",
            "workspace_id": "ws-1",
            "agent_profile": "default",
            "role": "operator",
            "workspace_path": ".",
            "policy_snapshot": {"production_hardening": True},
            "subject_type": "plugin",
            "trust_tier": "untrusted",
            "capability_grants": [],
        }
    )

    denied = await reg.execute("web_fetch", {"url": "https://example.com/"})
    assert denied.ok is False
    assert denied.error is not None
    assert denied.error.code == "capability_grant_denied"
    assert denied.meta.get("capability") == "network.fetch"


async def test_registry_denies_web_fetch_for_non_allowlisted_domain() -> None:
    cfg = Config.model_validate(
        convert_keys({"tools": {"policy": {"fetchAllowedDomains": ["example.com"]}}})
    )
    reg = ToolRegistry(policy=ToolPolicyEngine(default_deny_tools=set()))
    reg.register(_FetchTool())
    reg.set_security_policy(cfg.tools.policy)
    reg.set_request_context(
        {
            "channel": "cli",
            "sender_id": "user-1",
            "chat_id": "chat-1",
            "tenant_id": "tenant-a",
            "workspace_id": "ws-1",
            "agent_profile": "default",
            "role": "operator",
            "workspace_path": ".",
            "policy_snapshot": {"production_hardening": True},
        }
    )

    denied = await reg.execute("web_fetch", {"url": "https://evil.example.net/"})
    assert denied.ok is False
    assert denied.error is not None
    assert denied.error.code == "resource_scope_domain_denied"
    assert denied.meta.get("resource_scope", {}).get("domain") == "evil.example.net"


async def test_registry_denies_filesystem_access_outside_workspace() -> None:
    cfg = Config()
    reg = ToolRegistry(policy=ToolPolicyEngine(default_deny_tools=set()))
    reg.register(_ReadFileTool())
    reg.set_security_policy(cfg.tools.policy)
    reg.set_request_context(
        {
            "channel": "cli",
            "sender_id": "user-1",
            "chat_id": "chat-1",
            "tenant_id": "tenant-a",
            "workspace_id": "ws-1",
            "agent_profile": "default",
            "role": "operator",
            "workspace_path": "E:/nano-claw-public/tests",
            "policy_snapshot": {"production_hardening": True},
        }
    )

    denied = await reg.execute("read_file", {"path": "E:/nano-claw-public/pyproject.toml"})
    assert denied.ok is False
    assert denied.error is not None
    assert denied.error.code == "resource_scope_filesystem_workspace"
    assert denied.meta.get("policy_scope") == "resource"


async def test_registry_denies_filesystem_access_without_allowlisted_subpath_under_hardening() -> None:
    cfg = Config()
    reg = ToolRegistry(policy=ToolPolicyEngine(default_deny_tools=set()))
    reg.register(_ReadFileTool())
    reg.set_security_policy(cfg.tools.policy)
    reg.set_request_context(
        {
            "channel": "cli",
            "sender_id": "user-1",
            "chat_id": "chat-1",
            "tenant_id": "tenant-a",
            "workspace_id": "ws-1",
            "agent_profile": "default",
            "role": "operator",
            "workspace_path": "E:/nano-claw-public/tests",
            "policy_snapshot": {"production_hardening": True},
        }
    )

    denied = await reg.execute("read_file", {"path": "E:/nano-claw-public/tests/test_tool_policy_engine.py"})
    assert denied.ok is False
    assert denied.error is not None
    assert denied.error.code == "resource_scope_filesystem_unconfigured"


async def test_registry_denies_connector_write_without_explicit_policy() -> None:
    cfg = Config()
    reg = ToolRegistry(policy=ToolPolicyEngine(default_deny_tools=set()))
    reg.register(_ConnectorPostTool())
    reg.set_security_policy(cfg.tools.policy)
    reg.set_request_context(
        {
            "channel": "cli",
            "sender_id": "user-1",
            "chat_id": "chat-1",
            "tenant_id": "tenant-a",
            "workspace_id": "ws-1",
            "agent_profile": "default",
            "role": "operator",
            "workspace_path": ".",
            "policy_snapshot": {"production_hardening": True},
        }
    )

    denied = await reg.execute(
        "social_platform_post",
        {
            "connector_name": "moltbook",
            "action": "connector.record.create",
            "target_resource": "moltbook.posts",
        },
    )
    assert denied.ok is False
    assert denied.error is not None
    assert denied.error.code == "resource_scope_connector_unconfigured"
    assert denied.meta.get("capability") == "connector.record.create"


async def test_registry_denies_knowledge_without_explicit_allowlist_under_hardening() -> None:
    cfg = Config()
    reg = ToolRegistry(policy=ToolPolicyEngine(default_deny_tools=set()))
    reg.register(_KnowledgeTool())
    reg.set_security_policy(cfg.tools.policy)
    reg.set_request_context(
        {
            "channel": "cli",
            "sender_id": "user-1",
            "chat_id": "chat-1",
            "tenant_id": "tenant-a",
            "workspace_id": "ws-1",
            "agent_profile": "default",
            "role": "operator",
            "workspace_path": ".",
            "policy_snapshot": {"production_hardening": True},
        }
    )

    denied = await reg.execute("knowledge_search", {"notebook_id": "nb-1"})
    assert denied.ok is False
    assert denied.error is not None
    assert denied.error.code == "resource_scope_knowledge_unconfigured"


async def test_registry_denies_connector_cross_tenant_target() -> None:
    cfg = Config.model_validate(
        convert_keys(
            {
                "tools": {
                    "policy": {
                        "connectorAllowedNames": ["moltbook"],
                        "connectorAllowedActions": ["connector.record.create"],
                        "connectorAllowedTargetResources": ["moltbook.posts"],
                        "connectorAllowedTenants": ["tenant-a"],
                        "connectorAllowedWorkspaces": ["ws-1"],
                    }
                }
            }
        )
    )
    reg = ToolRegistry(policy=ToolPolicyEngine(default_deny_tools=set()))
    reg.register(_ConnectorPostTool())
    reg.set_security_policy(cfg.tools.policy)
    reg.set_request_context(
        {
            "channel": "cli",
            "sender_id": "user-1",
            "chat_id": "chat-1",
            "tenant_id": "tenant-a",
            "workspace_id": "ws-1",
            "agent_profile": "default",
            "role": "operator",
            "workspace_path": ".",
            "policy_snapshot": {"production_hardening": True},
        }
    )

    denied = await reg.execute(
        "social_platform_post",
        {
            "connector_name": "moltbook",
            "action": "connector.record.create",
            "target_resource": "moltbook.posts.comments",
            "tenant_id": "tenant-b",
            "workspace_id": "ws-1",
        },
    )
    assert denied.ok is False
    assert denied.error is not None
    assert denied.error.code == "resource_scope_connector_cross_tenant"
    assert denied.meta.get("matched_scope", {}).get("current_tenant") == "tenant-a"


def test_capability_policy_evaluator_reuses_connector_policy_for_channel_outbound() -> None:
    cfg = Config.model_validate(
        convert_keys(
            {
                "tools": {
                    "policy": {
                        "connectorAllowedNames": ["discord"],
                        "connectorAllowedActions": ["connector.message.send"],
                        "connectorAllowedTargetResources": ["channel.discord"],
                        "connectorAllowedTenants": ["tenant-a"],
                        "connectorAllowedWorkspaces": ["ws-1"],
                    }
                }
            }
        )
    )
    evaluator = CapabilityPolicyEvaluator(
        cfg.tools.policy,
        {
            "tenant_id": "tenant-a",
            "workspace_id": "ws-1",
            "policy_snapshot": {"production_hardening": True},
        },
    )

    allowed = evaluator.evaluate(
        connector_capability_request(
            capability="connector.message.send",
            connector_name="discord",
            target_resource="channel.discord.message",
            tenant_id="tenant-a",
            workspace_id="ws-1",
        )
    )
    assert allowed.allowed is True
    assert allowed.capability == "connector.message.send"

    denied = evaluator.evaluate(
        connector_capability_request(
            capability="connector.file.upload",
            connector_name="discord",
            target_resource="channel.discord.file",
            tenant_id="tenant-a",
            workspace_id="ws-1",
        )
    )
    assert denied.allowed is False
    assert denied.error_code == "resource_scope_connector_action"


def test_capability_policy_evaluator_denies_unconfigured_message_under_hardening() -> None:
    cfg = Config()
    evaluator = CapabilityPolicyEvaluator(
        cfg.tools.policy,
        {
            "channel": "cli",
            "tenant_id": "tenant-a",
            "workspace_id": "ws-1",
            "policy_snapshot": {"production_hardening": True},
        },
    )
    decision = evaluator.evaluate(
        CapabilityRequest(capability="message.send", resource_scope={"channel": "slack"})
    )
    assert decision.allowed is False
    assert decision.error_code == "resource_scope_message_unconfigured"


# ── tests: PolicyReason structured output ────────────────────────────────────


def test_policy_reason_str_returns_message() -> None:
    reason = PolicyReason(
        code="tool_policy_denied",
        tool="exec",
        trigger="session",
        message="Tool 'exec' denied by policy scope 'session'",
    )
    assert str(reason) == "Tool 'exec' denied by policy scope 'session'"


def test_policy_reason_to_dict() -> None:
    reason = PolicyReason(
        code="tool_policy_denied",
        tool="exec",
        trigger="session",
        message="Tool 'exec' denied by policy scope 'session'",
    )
    d = reason.to_dict()
    assert d == {
        "code": "tool_policy_denied",
        "tool": "exec",
        "trigger": "session",
        "message": "Tool 'exec' denied by policy scope 'session'",
    }


def test_evaluate_returns_policy_reason_on_scope_deny() -> None:
    engine = ToolPolicyEngine(default_deny_tools=set())
    engine.set_scope("channel", deny={"exec"})

    decision = engine.evaluate("exec")
    assert isinstance(decision.reason, PolicyReason)
    assert decision.reason.code == "tool_policy_denied"
    assert decision.reason.tool == "exec"
    assert decision.reason.trigger == "channel"
    assert "denied by policy scope" in decision.reason.message


def test_evaluate_returns_policy_reason_on_allowlist_allowed() -> None:
    engine = ToolPolicyEngine(default_deny_tools=set())
    engine.set_scope("session", allow={"read_file"})

    decision = engine.evaluate("read_file")
    assert isinstance(decision.reason, PolicyReason)
    assert decision.reason.code == "tool_policy_allowed"
    assert decision.reason.tool == "read_file"
    assert decision.reason.trigger == "allow_list"


def test_evaluate_returns_policy_reason_on_not_allowlisted() -> None:
    engine = ToolPolicyEngine(default_deny_tools=set())
    engine.set_scope("session", allow={"read_file"})

    decision = engine.evaluate("web_fetch")
    assert isinstance(decision.reason, PolicyReason)
    assert decision.reason.code == "tool_policy_not_allowlisted"
    assert decision.reason.tool == "web_fetch"
    assert decision.reason.trigger == "allow_list"


def test_evaluate_returns_policy_reason_on_default_deny() -> None:
    engine = ToolPolicyEngine(default_deny_tools={"exec"})

    decision = engine.evaluate("exec")
    assert isinstance(decision.reason, PolicyReason)
    assert decision.reason.code == "tool_policy_default_deny"
    assert decision.reason.tool == "exec"
    assert decision.reason.trigger == "default_deny"


def test_evaluate_returns_policy_reason_on_default_allow() -> None:
    engine = ToolPolicyEngine(default_deny_tools=set())

    decision = engine.evaluate("read_file")
    assert isinstance(decision.reason, PolicyReason)
    assert decision.reason.code == "tool_policy_allowed"
    assert decision.reason.tool == "read_file"
    assert decision.reason.trigger == "default"

"""Tests for zero-trust capability policy hardening gap fixes (GAP 1-4, 7-9)."""

from types import SimpleNamespace
from typing import Any

import pytest

from zen_claw.agent.tools.capability_policy import (
    CapabilityPolicyEvaluator,
    CapabilityRequest,
)
from zen_claw.agent.tools.result import ToolErrorKind


def _hardened_policy(**overrides: Any) -> SimpleNamespace:
    """Build a minimal hardened policy stub."""
    defaults = {
        "production_hardening": True,
        "legacy_compat": False,
        "filesystem_allowed_subpaths": [],
        "exec_allowed_working_dirs": [],
        "exec_allowed_command_prefixes": [],
        "sessions_allowed_working_dirs": [],
        "sessions_allowed_command_prefixes": [],
        "sessions_allowed_session_ops": [],
        "fetch_allowed_domains": [],
        "fetch_blocked_domains": [],
        "search_allowed_domains": [],
        "search_blocked_domains": [],
        "browser_allowed_domains": [],
        "browser_blocked_domains": [],
        "message_allowed_channels": [],
        "knowledge_allowed_tenants": [],
        "knowledge_allowed_notebooks": [],
        "connector_allowed_names": [],
        "connector_allowed_actions": [],
        "connector_allowed_target_resources": [],
        "connector_allowed_tenants": [],
        "connector_allowed_workspaces": [],
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _non_hardened_policy(**overrides: Any) -> SimpleNamespace:
    return _hardened_policy(production_hardening=False, **overrides)


# ── GAP-1: unknown capability → deny under hardening ────────────────


def test_unknown_capability_denied_under_hardening() -> None:
    ev = CapabilityPolicyEvaluator(_hardened_policy())
    decision = ev.evaluate(CapabilityRequest(capability="custom.foobar"))
    assert decision.allowed is False
    assert decision.error_code == "resource_scope_capability_unknown"


def test_unknown_capability_allowed_without_hardening() -> None:
    ev = CapabilityPolicyEvaluator(_non_hardened_policy())
    decision = ev.evaluate(CapabilityRequest(capability="custom.foobar"))
    assert decision.allowed is True


# ── GAP-2: policy=None → deny under hardening ───────────────────────


def test_null_policy_denied_when_snapshot_is_hardened() -> None:
    identity = {"policy_snapshot": {"production_hardening": True}}
    ev = CapabilityPolicyEvaluator(None, identity=identity)
    decision = ev.evaluate(CapabilityRequest(capability="exec.run"))
    assert decision.allowed is False
    assert decision.error_code == "resource_scope_policy_missing"


def test_null_policy_allowed_when_snapshot_not_hardened() -> None:
    identity = {"policy_snapshot": {"production_hardening": False}}
    ev = CapabilityPolicyEvaluator(None, identity=identity)
    decision = ev.evaluate(CapabilityRequest(capability="exec.run"))
    assert decision.allowed is True


def test_null_policy_allowed_when_no_snapshot() -> None:
    ev = CapabilityPolicyEvaluator(None)
    decision = ev.evaluate(CapabilityRequest(capability="exec.run"))
    assert decision.allowed is True


# ── GAP-3: request=None → deny under hardening ──────────────────────


def test_null_request_denied_under_hardening() -> None:
    ev = CapabilityPolicyEvaluator(_hardened_policy())
    decision = ev.evaluate(None)
    assert decision.allowed is False
    assert decision.error_code == "resource_scope_request_missing"


def test_null_request_allowed_without_hardening() -> None:
    ev = CapabilityPolicyEvaluator(_non_hardened_policy())
    decision = ev.evaluate(None)
    assert decision.allowed is True


# ── GAP-4: filesystem empty path/workspace → deny under hardening ───


def test_filesystem_empty_path_denied_under_hardening() -> None:
    ev = CapabilityPolicyEvaluator(_hardened_policy())
    decision = ev.evaluate(
        CapabilityRequest(
            capability="filesystem.access",
            resource_scope={"filesystem_path": "", "workspace_path": "/work"},
        )
    )
    assert decision.allowed is False
    assert decision.error_code == "resource_scope_filesystem_path_missing"


def test_filesystem_empty_workspace_denied_under_hardening() -> None:
    ev = CapabilityPolicyEvaluator(_hardened_policy())
    decision = ev.evaluate(
        CapabilityRequest(
            capability="filesystem.access",
            resource_scope={"filesystem_path": "/some/file", "workspace_path": ""},
        )
    )
    assert decision.allowed is False
    assert decision.error_code == "resource_scope_filesystem_path_missing"


def test_filesystem_empty_path_allowed_without_hardening() -> None:
    ev = CapabilityPolicyEvaluator(_non_hardened_policy())
    decision = ev.evaluate(
        CapabilityRequest(
            capability="filesystem.access",
            resource_scope={"filesystem_path": "", "workspace_path": ""},
        )
    )
    assert decision.allowed is True


# ── GAP-7: connector allowlist incomplete dimensions → deny ─────────


def test_connector_incomplete_dimensions_denied_under_hardening() -> None:
    """Only names configured, actions and targets missing → deny."""
    policy = _hardened_policy(connector_allowed_names=["slack"])
    ev = CapabilityPolicyEvaluator(policy)
    decision = ev.evaluate(
        CapabilityRequest(
            capability="connector.record.create",
            resource_scope={
                "connector_name": "slack",
                "action": "connector.record.create",
                "target_resource": "channel.general",
                "tenant_id": "t1",
                "workspace_id": "ws1",
                "current_tenant_id": "t1",
                "current_workspace_id": "ws1",
            },
        )
    )
    assert decision.allowed is False
    assert decision.error_code == "resource_scope_connector_incomplete"


def test_connector_complete_dimensions_allowed_under_hardening() -> None:
    """All three core dimensions configured → allow."""
    policy = _hardened_policy(
        connector_allowed_names=["slack"],
        connector_allowed_actions=["connector.record.create"],
        connector_allowed_target_resources=["channel.general"],
    )
    ev = CapabilityPolicyEvaluator(policy)
    decision = ev.evaluate(
        CapabilityRequest(
            capability="connector.record.create",
            resource_scope={
                "connector_name": "slack",
                "action": "connector.record.create",
                "target_resource": "channel.general",
                "tenant_id": "t1",
                "workspace_id": "ws1",
                "current_tenant_id": "t1",
                "current_workspace_id": "ws1",
            },
        )
    )
    assert decision.allowed is True


def test_connector_incomplete_dimensions_allowed_without_hardening() -> None:
    """Without hardening, partial dimensions are allowed."""
    policy = _non_hardened_policy(connector_allowed_names=["slack"])
    ev = CapabilityPolicyEvaluator(policy)
    decision = ev.evaluate(
        CapabilityRequest(
            capability="connector.record.create",
            resource_scope={
                "connector_name": "slack",
                "action": "connector.record.create",
                "target_resource": "channel.general",
                "tenant_id": "t1",
                "workspace_id": "ws1",
                "current_tenant_id": "t1",
                "current_workspace_id": "ws1",
            },
        )
    )
    assert decision.allowed is True


# ── GAP-8: blocked_domains for fetch/search ──────────────────────────


def test_fetch_blocked_domain_denied() -> None:
    policy = _non_hardened_policy(fetch_blocked_domains=["evil.com"])
    ev = CapabilityPolicyEvaluator(policy)
    decision = ev.evaluate(
        CapabilityRequest(
            capability="network.fetch",
            resource_scope={"url": "https://evil.com/payload"},
        )
    )
    assert decision.allowed is False
    assert decision.error_code == "resource_scope_domain_blocked"


def test_fetch_blocked_subdomain_denied() -> None:
    policy = _non_hardened_policy(fetch_blocked_domains=["evil.com"])
    ev = CapabilityPolicyEvaluator(policy)
    decision = ev.evaluate(
        CapabilityRequest(
            capability="network.fetch",
            resource_scope={"url": "https://sub.evil.com/x"},
        )
    )
    assert decision.allowed is False
    assert decision.error_code == "resource_scope_domain_blocked"


def test_search_blocked_domain_denied() -> None:
    policy = _non_hardened_policy(search_blocked_domains=["evil.com"])
    ev = CapabilityPolicyEvaluator(policy)
    decision = ev.evaluate(
        CapabilityRequest(
            capability="network.search",
            resource_scope={"domain": "evil.com"},
        )
    )
    assert decision.allowed is False
    assert decision.error_code == "resource_scope_domain_blocked"


def test_blocked_domain_takes_precedence_over_allowed() -> None:
    """Blocked domain check runs before allowed domain check."""
    policy = _non_hardened_policy(
        fetch_allowed_domains=["evil.com"],
        fetch_blocked_domains=["evil.com"],
    )
    ev = CapabilityPolicyEvaluator(policy)
    decision = ev.evaluate(
        CapabilityRequest(
            capability="network.fetch",
            resource_scope={"url": "https://evil.com/x"},
        )
    )
    assert decision.allowed is False
    assert decision.error_code == "resource_scope_domain_blocked"


def test_non_blocked_domain_allowed() -> None:
    policy = _non_hardened_policy(fetch_blocked_domains=["evil.com"])
    ev = CapabilityPolicyEvaluator(policy)
    decision = ev.evaluate(
        CapabilityRequest(
            capability="network.fetch",
            resource_scope={"url": "https://good.com/x"},
        )
    )
    assert decision.allowed is True


# ── _is_hardened() helper ────────────────────────────────────────────


def test_is_hardened_from_policy() -> None:
    ev = CapabilityPolicyEvaluator(_hardened_policy())
    assert ev._is_hardened() is True

    ev2 = CapabilityPolicyEvaluator(_non_hardened_policy())
    assert ev2._is_hardened() is False


def test_is_hardened_from_snapshot_when_policy_none() -> None:
    ev = CapabilityPolicyEvaluator(
        None, identity={"policy_snapshot": {"production_hardening": True}}
    )
    assert ev._is_hardened() is True

    ev2 = CapabilityPolicyEvaluator(
        None, identity={"policy_snapshot": {"production_hardening": False}}
    )
    assert ev2._is_hardened() is False

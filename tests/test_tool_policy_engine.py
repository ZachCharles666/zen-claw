from typing import Any

from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.policy import ToolPolicyEngine
from zen_claw.agent.tools.registry import ToolRegistry
from zen_claw.agent.tools.result import ToolErrorKind


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

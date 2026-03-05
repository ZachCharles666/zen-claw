from pathlib import Path
from typing import Any

from zen_claw.agent.subagent import SUBAGENT_HARD_DENY_TOOLS, SubagentManager
from zen_claw.agent.tools.policy import ToolPolicyEngine
from zen_claw.agent.tools.registry import ToolRegistry
from zen_claw.agent.tools.spawn import SpawnTool
from zen_claw.config.loader import convert_keys
from zen_claw.config.schema import Config, ToolPolicyConfig


def test_subagent_hard_deny_contains_sensitive_tools() -> None:
    for name in [
        "spawn",
        "message",
        "cron",
        "sessions_spawn",
        "sessions_list",
        "sessions_kill",
        "sessions_read",
        "sessions_write",
        "sessions_signal",
        "sessions_resize",
    ]:
        assert name in SUBAGENT_HARD_DENY_TOOLS


def test_subagent_hard_deny_overrides_allowlist() -> None:
    engine = ToolPolicyEngine(default_deny_tools=set())
    engine.set_scope("subagent", allow={"*"})
    engine.set_scope("subagent_hard_deny", deny=SUBAGENT_HARD_DENY_TOOLS)

    denied = engine.evaluate("spawn")
    assert denied.allowed is False
    assert denied.code == "tool_policy_denied"
    assert denied.scope == "subagent_hard_deny"


def test_config_parses_allow_subagent_sensitive_tools() -> None:
    raw = {"tools": {"policy": {"allowSubagentSensitiveTools": True}}}
    config = Config.model_validate(convert_keys(raw))
    assert config.tools.policy.allow_subagent_sensitive_tools is True


class _DummyProvider:
    def get_default_model(self) -> str:
        return "dummy-model"


class _DummyBus:
    async def publish_inbound(self, msg: Any) -> None:
        return None


class _FakeSpawnManager:
    async def spawn(self, **kwargs: Any) -> str:
        return "spawned"


def test_subagent_sensitive_override_requires_env(monkeypatch) -> None:
    policy = ToolPolicyConfig()
    policy.allow_subagent_sensitive_tools = True
    sub = SubagentManager(
        provider=_DummyProvider(),  # type: ignore[arg-type]
        workspace=Path("."),
        bus=_DummyBus(),  # type: ignore[arg-type]
        tool_policy_config=policy,
    )

    monkeypatch.delenv("zen-claw_ALLOW_SUBAGENT_SENSITIVE_TOOLS", raising=False)
    assert sub._allow_sensitive_override_enabled() is False

    monkeypatch.setenv("zen-claw_ALLOW_SUBAGENT_SENSITIVE_TOOLS", "true")
    assert sub._allow_sensitive_override_enabled() is True


async def test_subagent_sensitive_override_without_env_still_denies_spawn(monkeypatch) -> None:
    policy = ToolPolicyConfig()
    policy.default_deny_tools = []
    policy.allow_subagent_sensitive_tools = True
    policy.subagent.allow = ["*"]
    policy.subagent.deny = []

    sub = SubagentManager(
        provider=_DummyProvider(),  # type: ignore[arg-type]
        workspace=Path("."),
        bus=_DummyBus(),  # type: ignore[arg-type]
        tool_policy_config=policy,
    )
    monkeypatch.delenv("zen-claw_ALLOW_SUBAGENT_SENSITIVE_TOOLS", raising=False)

    reg = ToolRegistry(policy=ToolPolicyEngine(default_deny_tools=set()))
    reg.register(SpawnTool(manager=_FakeSpawnManager()))  # type: ignore[arg-type]
    sub._apply_subagent_policy(reg)

    result = await reg.execute("spawn", {"task": "x"})
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "tool_policy_denied"

from pathlib import Path
from typing import Any

from zen_claw.agent.subagent import SubagentManager
from zen_claw.agent.tools.registry import ToolRegistry
from zen_claw.agent.tools.result import ToolErrorKind
from zen_claw.agent.tools.spawn import SpawnTool
from zen_claw.config.schema import ToolPolicyConfig


class _DummyProvider:
    def get_default_model(self) -> str:
        return "dummy-model"


class _DummyBus:
    async def publish_inbound(self, msg: Any) -> None:
        return None


class _FakeSpawnManager:
    def __init__(self) -> None:
        self.calls = 0

    async def spawn(self, **kwargs: Any) -> str:
        self.calls += 1
        return "spawned"


async def test_subagent_guardrail_blocks_spawn_end_to_end() -> None:
    policy = ToolPolicyConfig()
    policy.subagent.allow = ["*"]
    policy.allow_subagent_sensitive_tools = False

    sub = SubagentManager(
        provider=_DummyProvider(),  # type: ignore[arg-type]
        workspace=Path("."),
        bus=_DummyBus(),  # type: ignore[arg-type]
        tool_policy_config=policy,
    )

    fake = _FakeSpawnManager()
    reg = ToolRegistry()
    reg.register(SpawnTool(manager=fake))  # type: ignore[arg-type]
    sub._apply_subagent_policy(reg)

    result = await reg.execute("spawn", {"task": "do something"})
    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.PERMISSION
    assert result.error.code == "tool_policy_denied"
    assert fake.calls == 0

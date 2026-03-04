from pathlib import Path

import pytest

from zen_claw.agent.loop import AgentLoop
from zen_claw.bus.queue import MessageBus
from zen_claw.config.schema import ToolPolicyConfig
from zen_claw.providers.base import LLMProvider, LLMResponse


class _DummyProvider(LLMProvider):
    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "dummy-model"


def _make_loop(tmp_path: Path, monkeypatch, mode: str) -> AgentLoop:
    class _NoopSessionManager:
        def __init__(self, workspace):
            self.workspace = workspace

    monkeypatch.setattr("zen_claw.agent.loop.SessionManager", _NoopSessionManager)
    policy = ToolPolicyConfig()
    policy.default_deny_tools = []
    policy.agent.allow = ["*"]
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_DummyProvider(),
        workspace=tmp_path,
        tool_policy_config=policy,
        skill_names=[],
        skill_permissions_mode=mode,
    )
    return loop


def test_skill_scope_runtime_gate_enforce_rejects_mismatch(tmp_path: Path, monkeypatch) -> None:
    loop = _make_loop(tmp_path, monkeypatch, mode="enforce")
    loop.skill_names = ["s1"]
    loop.context.skills.get_skill_manifest = lambda name: (  # type: ignore[method-assign]
        {
            "name": name,
            "permissions": ["exec"],
            "scopes": ["filesystem"],
        },
        [],
    )
    with pytest.raises(ValueError):
        loop._apply_skill_permission_gate()


@pytest.mark.asyncio
async def test_skill_scope_runtime_gate_warn_clamps_uncovered_permissions(tmp_path: Path, monkeypatch) -> None:
    sample = tmp_path / "a.txt"
    sample.write_text("ok", encoding="utf-8")
    loop = _make_loop(tmp_path, monkeypatch, mode="warn")
    loop.skill_names = ["s1"]
    loop.context.skills.get_skill_manifest = lambda name: (  # type: ignore[method-assign]
        {
            "name": name,
            "permissions": ["exec", "read_file"],
            "scopes": ["filesystem"],
        },
        [],
    )
    loop._apply_skill_permission_gate()

    denied = await loop.tools.execute("exec", {"command": "echo hi"})
    assert denied.ok is False
    assert denied.error is not None
    assert denied.error.code == "skill_permission_denied"

    allowed = await loop.tools.execute("read_file", {"path": str(sample)})
    assert allowed.ok is True

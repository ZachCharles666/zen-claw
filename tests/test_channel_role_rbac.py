from pathlib import Path

from zen_claw.agent.loop import AgentLoop
from zen_claw.bus.queue import MessageBus
from zen_claw.config.schema import ToolPolicyConfig
from zen_claw.providers.base import LLMProvider, LLMResponse


class _DummyProvider(LLMProvider):
    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "dummy"


def _build_loop(tmp_path: Path, monkeypatch) -> AgentLoop:
    class _NoopSessionManager:
        def __init__(self, workspace):
            self.workspace = workspace

    monkeypatch.setattr("zen_claw.agent.loop.SessionManager", _NoopSessionManager)
    policy = ToolPolicyConfig()
    policy.default_deny_tools = []
    policy.agent.allow = ["*"]
    return AgentLoop(
        bus=MessageBus(),
        provider=_DummyProvider(),
        workspace=tmp_path,
        tool_policy_config=policy,
    )


async def test_channel_role_user_denies_exec(tmp_path: Path, monkeypatch) -> None:
    loop = _build_loop(tmp_path, monkeypatch)
    loop._apply_channel_role_tool_policy({"channel_role": "user"})
    result = await loop.tools.execute("exec", {"command": "echo hi"})
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "tool_policy_denied"
    assert result.meta.get("policy_scope") == "channel_role"


async def test_channel_role_user_allows_read_file(tmp_path: Path, monkeypatch) -> None:
    sample = tmp_path / "a.txt"
    sample.write_text("hello", encoding="utf-8")
    loop = _build_loop(tmp_path, monkeypatch)
    loop._apply_channel_role_tool_policy({"channel_role": "user"})
    result = await loop.tools.execute("read_file", {"path": str(sample)})
    assert result.ok is True
    assert "hello" in result.content


async def test_channel_role_admin_clears_role_scope(tmp_path: Path, monkeypatch) -> None:
    loop = _build_loop(tmp_path, monkeypatch)
    loop._apply_channel_role_tool_policy({"channel_role": "user"})
    loop._apply_channel_role_tool_policy({"channel_role": "admin"})
    result = await loop.tools.execute("exec", {})
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "invalid_parameters"

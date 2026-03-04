from pathlib import Path

import pytest

from zen_claw.agent.pool import AgentPool, resolve_agent_workspace
from zen_claw.bus.queue import MessageBus
from zen_claw.config.schema import Config
from zen_claw.providers.base import LLMProvider, LLMResponse


class _Provider(LLMProvider):
    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        return LLMResponse(content="ok", tool_calls=[], usage={})

    def get_default_model(self) -> str:
        return "test-model"


def test_resolve_agent_workspace_default_root() -> None:
    p = resolve_agent_workspace("agent-a")
    assert str(p).replace("\\", "/").endswith("/.zen-claw/workspaces/agent-a")


@pytest.mark.asyncio
async def test_agent_pool_creates_isolated_workspaces(tmp_path: Path, monkeypatch) -> None:
    cfg = Config()
    cfg.agents.defaults.model = "test-model"
    provider = _Provider(api_key=None, api_base=None)
    bus = MessageBus()

    monkeypatch.setattr(
        "zen_claw.agent.pool.resolve_agent_workspace",
        lambda aid, base_dir=None: (tmp_path / "workspaces" / aid).resolve(),
    )
    pool = AgentPool(config=cfg, bus=bus, provider=provider)
    a = await pool.get_or_create("alpha")
    b = await pool.get_or_create("beta")
    assert a.workspace != b.workspace
    assert (tmp_path / "workspaces" / "alpha").exists()
    assert (tmp_path / "workspaces" / "beta").exists()

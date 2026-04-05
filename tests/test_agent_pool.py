from pathlib import Path

import pytest

from zen_claw.agent.pool import (
    AgentPool,
    list_agent_profiles,
    resolve_agent_profile,
    resolve_agent_workspace,
)
from zen_claw.agent.skills import SkillsLoader
from zen_claw.bus.queue import MessageBus
from zen_claw.config.loader import convert_keys
from zen_claw.config.schema import Config
from zen_claw.providers.base import LLMProvider, LLMResponse


@pytest.fixture(autouse=True)
def mock_skills_loader(monkeypatch):
    # Mock mapping and time to prevent potentially slow/hanging I/O or crypto in CI
    monkeypatch.setattr(SkillsLoader, "_load_skill_mapping", lambda self: None)
    monkeypatch.setattr(SkillsLoader, "_save_skill_mapping", lambda self: None)
    monkeypatch.setattr(SkillsLoader, "_now_ts", lambda self: 1000.0)


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


def test_resolve_agent_profile_applies_registered_overrides(tmp_path: Path) -> None:
    cfg = Config.model_validate(
        convert_keys(
        {
            "agents": {
                "defaults": {
                    "workspace": str(tmp_path / "default-ws"),
                    "model": "default-model",
                    "enablePlanning": True,
                },
                    "profiles": {
                        "finance_writer": {
                            "displayName": "Finance Writer",
                            "description": "Finance marketing profile",
                            "workspace": str(tmp_path / "finance-ws"),
                            "model": "deepseek-chat",
                            "enablePlanning": False,
                            "skillNames": ["content_gen", "rag_retrieve"],
                            "allowedTools": ["read_file", "web_fetch"],
                            "deniedTools": ["exec"],
                            "devProfile": True,
                            "trustedLocalOnly": True,
                            "allowedChannels": ["cli"],
                        }
                },
            }
        }
        )
    )

    profile = resolve_agent_profile(cfg, "finance_writer")
    assert profile.agent_id == "finance_writer"
    assert profile.display_name == "Finance Writer"
    assert profile.description == "Finance marketing profile"
    assert profile.workspace == (tmp_path / "finance-ws").resolve()
    assert profile.model == "deepseek-chat"
    assert profile.enable_planning is False
    assert profile.skill_names == ["content_gen", "rag_retrieve"]
    assert profile.allowed_tools == ["read_file", "web_fetch"]
    assert profile.denied_tools == ["exec"]
    assert profile.dev_profile is True
    assert profile.trusted_local_only is True
    assert profile.allowed_channels == ["cli"]


def test_list_agent_profiles_includes_default_and_registered(tmp_path: Path) -> None:
    cfg = Config.model_validate(
        convert_keys(
        {
            "agents": {
                "defaults": {"workspace": str(tmp_path / "default-ws"), "model": "default-model"},
                "profiles": {"finance_writer": {"displayName": "Finance Writer"}},
            }
        }
        )
    )

    rows = list_agent_profiles(cfg)
    assert [row.agent_id for row in rows] == ["default", "finance_writer"]


@pytest.mark.asyncio
async def test_agent_pool_uses_profile_specific_workspace_and_provider(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = Config.model_validate(
        convert_keys(
        {
            "agents": {
                "defaults": {"workspace": str(tmp_path / "default-ws"), "model": "default-model"},
                    "profiles": {
                        "finance_writer": {
                            "workspace": str(tmp_path / "finance-ws"),
                            "model": "deepseek-chat",
                            "enablePlanning": False,
                            "skillNames": ["content_gen"],
                        }
                    },
                }
        }
        )
    )
    bus = MessageBus()
    provider = _Provider(api_key=None, api_base=None)
    created_models: list[str | None] = []

    def _provider_factory(model: str | None) -> _Provider:
        created_models.append(model)
        return provider

    monkeypatch.setattr(
        "zen_claw.agent.pool.resolve_agent_workspace",
        lambda aid, base_dir=None: (tmp_path / "workspaces" / aid).resolve(),
    )
    pool = AgentPool(config=cfg, bus=bus, provider_factory=_provider_factory)
    loop = await pool.get_or_create("finance_writer")
    assert loop.workspace == (tmp_path / "finance-ws").resolve()
    assert loop.model == "deepseek-chat"
    assert loop.enable_planning is False
    assert loop.skill_names == ["content_gen"]
    assert created_models == ["deepseek-chat"]

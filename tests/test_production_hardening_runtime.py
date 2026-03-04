from pathlib import Path
from typing import Any

from zen_claw.agent.loop import AgentLoop
from zen_claw.agent.tools.shell import ExecTool
from zen_claw.agent.tools.web import WebFetchTool, WebSearchTool
from zen_claw.bus.queue import MessageBus
from zen_claw.config.loader import convert_keys
from zen_claw.config.schema import Config


class _DummyProvider:
    def get_default_model(self) -> str:
        return "dummy-model"

    async def chat(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("not used in this test")


def _build_strict_config() -> Config:
    raw = {
        "tools": {
            "policy": {"productionHardening": True},
            "network": {
                "exec": {
                    "mode": "sidecar",
                    "sidecarFallbackToLocal": True,
                },
                "search": {
                    "mode": "proxy",
                    "proxyFallbackToLocal": True,
                },
                "fetch": {
                    "mode": "proxy",
                    "proxyFallbackToLocal": True,
                },
            },
        }
    }
    return Config.model_validate(convert_keys(raw))


def test_production_hardening_enforced_in_agent_runtime_tool_instances(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    cfg = _build_strict_config()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_DummyProvider(),  # type: ignore[arg-type]
        workspace=Path("."),
        brave_api_key=cfg.tools.effective_search().api_key or None,
        web_search_config=cfg.tools.effective_search(),
        web_fetch_config=cfg.tools.effective_fetch(),
        exec_config=cfg.tools.effective_exec(),
        tool_policy_config=cfg.tools.policy,
        restrict_to_workspace=cfg.tools.restrict_to_workspace,
    )

    exec_tool = loop.tools.get("exec")
    assert isinstance(exec_tool, ExecTool)
    assert exec_tool.sidecar_fallback_to_local is False

    search_tool = loop.tools.get("web_search")
    assert isinstance(search_tool, WebSearchTool)
    assert search_tool.proxy_fallback_to_local is False

    fetch_tool = loop.tools.get("web_fetch")
    assert isinstance(fetch_tool, WebFetchTool)
    assert fetch_tool.proxy_fallback_to_local is False

    assert cfg.tools.policy.allow_subagent_sensitive_tools is False



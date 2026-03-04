"""Multi-agent loop pool with per-agent isolated workspace."""

from __future__ import annotations

import asyncio
from pathlib import Path

from zen_claw.agent.loop import AgentLoop
from zen_claw.bus.queue import MessageBus
from zen_claw.config.schema import Config
from zen_claw.providers.base import LLMProvider


def resolve_agent_workspace(agent_id: str, base_dir: Path | None = None) -> Path:
    """Resolve isolated workspace path: ~/.zen-claw/workspaces/<agent_id>/."""
    aid = str(agent_id or "").strip().lower() or "default"
    root = Path(base_dir) if base_dir else (Path.home() / ".zen-claw" / "workspaces")
    return (root / aid).expanduser().resolve()


class AgentPool:
    """Lazily create and cache AgentLoop instances by agent id."""

    def __init__(
        self,
        *,
        config: Config,
        bus: MessageBus,
        provider: LLMProvider,
    ):
        self.config = config
        self.bus = bus
        self.provider = provider
        self._agents: dict[str, AgentLoop] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(self, agent_id: str) -> AgentLoop:
        aid = str(agent_id or "").strip().lower() or "default"
        if aid in self._agents:
            return self._agents[aid]
        async with self._lock:
            if aid in self._agents:
                return self._agents[aid]
            loop = self._build_loop_for_agent(aid)
            self._agents[aid] = loop
            return loop

    def all_loops(self) -> list[AgentLoop]:
        return list(self._agents.values())

    def _build_loop_for_agent(self, agent_id: str) -> AgentLoop:
        exec_cfg = self.config.tools.effective_exec()
        search_cfg = self.config.tools.effective_search()
        fetch_cfg = self.config.tools.effective_fetch()
        browser_cfg = self.config.tools.effective_browser()
        workspace = resolve_agent_workspace(agent_id)
        workspace.mkdir(parents=True, exist_ok=True)
        return AgentLoop(
            bus=self.bus,
            provider=self.provider,
            workspace=workspace,
            model=self.config.agents.defaults.model,
            vision_model=self.config.agents.defaults.vision_model or None,
            memory_recall_mode=self.config.agents.defaults.memory_recall_mode,
            enable_planning=self.config.agents.defaults.enable_planning,
            max_reflections=self.config.agents.defaults.max_reflections,
            auto_parameter_rewrite=self.config.agents.defaults.auto_parameter_rewrite,
            max_context_tokens=self.config.agents.defaults.max_tokens,
            max_iterations=self.config.agents.defaults.max_tool_iterations,
            brave_api_key=search_cfg.api_key or None,
            web_search_config=search_cfg,
            web_fetch_config=fetch_cfg,
            browser_config=browser_cfg,
            exec_config=exec_cfg,
            tool_policy_config=self.config.tools.policy,
            restrict_to_workspace=self.config.tools.restrict_to_workspace,
            allowed_models=self.config.agents.defaults.allowed_models,
            compression_trigger_ratio=self.config.agents.defaults.compression_trigger_ratio,
            compression_hysteresis_ratio=self.config.agents.defaults.compression_hysteresis_ratio,
            compression_cooldown_turns=self.config.agents.defaults.compression_cooldown_turns,
        )

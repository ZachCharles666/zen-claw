"""Multi-agent loop pool with per-agent isolated workspace."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from zen_claw.agent.loop import AgentLoop
from zen_claw.bus.queue import MessageBus
from zen_claw.config.schema import AgentDefaults, AgentProfileConfig, Config
from zen_claw.providers.base import LLMProvider


def resolve_agent_workspace(agent_id: str, base_dir: Path | None = None) -> Path:
    """Resolve isolated workspace path: ~/.zen-claw/workspaces/<agent_id>/."""
    aid = str(agent_id or "").strip().lower() or "default"
    root = Path(base_dir) if base_dir else (Path.home() / ".zen-claw" / "workspaces")
    return (root / aid).expanduser().resolve()


def normalize_agent_id(agent_id: str) -> str:
    """Normalize agent/profile identifiers to a stable internal form."""
    normalized = re.sub(r"_+", "_", str(agent_id or "").strip().lower())
    return normalized or "default"


@dataclass(frozen=True)
class ResolvedAgentProfile:
    """Effective runtime configuration for one agent profile."""

    agent_id: str
    display_name: str
    description: str
    system_prompt: str
    system_prompt_file: str
    allowed_tools: list[str] | None
    denied_tools: list[str] | None
    skill_names: list[str]
    workspace: Path
    model: str
    vision_model: str
    thinking_model: str
    fallback_model: str
    cost_model: str
    stability_model: str
    intent_model_overrides: dict[str, str]
    task_type_model_overrides: dict[str, str]
    memory_recall_mode: str
    enable_planning: bool
    max_reflections: int
    auto_parameter_rewrite: bool
    skill_permissions_mode: str
    max_tokens: int
    compression_trigger_ratio: float
    compression_hysteresis_ratio: float
    compression_cooldown_turns: int
    max_tool_iterations: int
    allowed_models: list[str]
    is_registered: bool
    dev_profile: bool
    trusted_local_only: bool
    allowed_channels: list[str]


def _resolve_profile_workspace(
    agent_id: str,
    *,
    defaults: AgentDefaults,
    profile: AgentProfileConfig | None,
) -> Path:
    workspace = str(profile.workspace if profile else "").strip()
    if workspace:
        return Path(workspace).expanduser().resolve()
    if agent_id == "default":
        return Path(defaults.workspace).expanduser().resolve()
    return resolve_agent_workspace(agent_id)


def resolve_agent_profile(config: Config, agent_id: str) -> ResolvedAgentProfile:
    """Resolve effective per-agent runtime configuration from defaults + profile overrides."""
    aid = normalize_agent_id(agent_id)
    defaults = config.agents.defaults
    profile = config.agents.profiles.get(aid)
    display_name = str(profile.display_name if profile else "").strip() or (
        "Default Agent" if aid == "default" else aid
    )
    description = str(profile.description if profile else "").strip()
    return ResolvedAgentProfile(
        agent_id=aid,
        display_name=display_name,
        description=description,
        system_prompt=str(profile.system_prompt if profile else "").strip(),
        system_prompt_file=str(profile.system_prompt_file if profile else "").strip(),
        allowed_tools=list(profile.allowed_tools) if profile and profile.allowed_tools is not None else None,
        denied_tools=list(profile.denied_tools) if profile and profile.denied_tools is not None else None,
        skill_names=(
            list(profile.skill_names)
            if profile and profile.skill_names is not None and len(profile.skill_names) > 0
            else list(defaults.skill_names)
        ),
        workspace=_resolve_profile_workspace(aid, defaults=defaults, profile=profile),
        model=str(profile.model if profile else "").strip() or defaults.model,
        vision_model=str(profile.vision_model if profile else "").strip() or defaults.vision_model,
        thinking_model=str(profile.thinking_model if profile else "").strip()
        or defaults.thinking_model,
        fallback_model=str(profile.fallback_model if profile else "").strip()
        or defaults.fallback_model,
        cost_model=str(profile.cost_model if profile else "").strip() or defaults.cost_model,
        stability_model=str(profile.stability_model if profile else "").strip()
        or defaults.stability_model,
        intent_model_overrides=(
            dict(profile.intent_model_overrides)
            if profile and profile.intent_model_overrides
            else dict(defaults.intent_model_overrides)
        ),
        task_type_model_overrides=(
            dict(profile.task_type_model_overrides)
            if profile and profile.task_type_model_overrides
            else dict(defaults.task_type_model_overrides)
        ),
        memory_recall_mode=(
            profile.memory_recall_mode if profile and profile.memory_recall_mode else defaults.memory_recall_mode
        ),
        enable_planning=(
            profile.enable_planning if profile and profile.enable_planning is not None else defaults.enable_planning
        ),
        max_reflections=(
            profile.max_reflections if profile and profile.max_reflections is not None else defaults.max_reflections
        ),
        auto_parameter_rewrite=(
            profile.auto_parameter_rewrite
            if profile and profile.auto_parameter_rewrite is not None
            else defaults.auto_parameter_rewrite
        ),
        skill_permissions_mode=(
            profile.skill_permissions_mode
            if profile and profile.skill_permissions_mode is not None
            else defaults.skill_permissions_mode
        ),
        max_tokens=(
            profile.max_tokens if profile and profile.max_tokens is not None else defaults.max_tokens
        ),
        compression_trigger_ratio=(
            profile.compression_trigger_ratio
            if profile and profile.compression_trigger_ratio is not None
            else defaults.compression_trigger_ratio
        ),
        compression_hysteresis_ratio=(
            profile.compression_hysteresis_ratio
            if profile and profile.compression_hysteresis_ratio is not None
            else defaults.compression_hysteresis_ratio
        ),
        compression_cooldown_turns=(
            profile.compression_cooldown_turns
            if profile and profile.compression_cooldown_turns is not None
            else defaults.compression_cooldown_turns
        ),
        max_tool_iterations=(
            profile.max_tool_iterations
            if profile and profile.max_tool_iterations is not None
            else defaults.max_tool_iterations
        ),
        allowed_models=(
            list(profile.allowed_models)
            if profile and profile.allowed_models is not None
            else list(defaults.allowed_models)
        ),
        is_registered=profile is not None or aid == "default",
        dev_profile=bool(profile.dev_profile) if profile else False,
        trusted_local_only=bool(profile.trusted_local_only) if profile else False,
        allowed_channels=list(profile.allowed_channels) if profile else [],
    )


def list_agent_profiles(config: Config) -> list[ResolvedAgentProfile]:
    """List registered profiles, always including the implicit default profile."""
    profile_ids = {"default", *config.agents.profiles.keys()}
    return [resolve_agent_profile(config, agent_id) for agent_id in sorted(profile_ids)]


class AgentPool:
    """Lazily create and cache AgentLoop instances by agent id."""

    def __init__(
        self,
        *,
        config: Config,
        bus: MessageBus,
        provider: LLMProvider | None = None,
        provider_factory: Callable[[str | None], LLMProvider] | None = None,
    ):
        self.config = config
        self.bus = bus
        self.provider = provider
        self.provider_factory = provider_factory
        self._agents: dict[str, AgentLoop] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(self, agent_id: str) -> AgentLoop:
        aid = normalize_agent_id(agent_id)
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
        profile = resolve_agent_profile(self.config, agent_id)
        exec_cfg = self.config.tools.effective_exec()
        search_cfg = self.config.tools.effective_search()
        fetch_cfg = self.config.tools.effective_fetch()
        connector_cfg = self.config.tools.network.connector
        browser_cfg = self.config.tools.effective_browser()
        provider = self._build_provider_for_agent(profile)
        workspace = profile.workspace
        workspace.mkdir(parents=True, exist_ok=True)
        return AgentLoop(
            bus=self.bus,
            provider=provider,
            workspace=workspace,
            model=profile.model,
            system_prompt_override=profile.system_prompt or None,
            system_prompt_file=profile.system_prompt_file or None,
            profile_allowed_tools=profile.allowed_tools,
            profile_denied_tools=profile.denied_tools,
            skill_names=profile.skill_names,
            vision_model=profile.vision_model or None,
            memory_recall_mode=profile.memory_recall_mode,
            enable_planning=profile.enable_planning,
            max_reflections=profile.max_reflections,
            auto_parameter_rewrite=profile.auto_parameter_rewrite,
            max_context_tokens=profile.max_tokens,
            max_iterations=profile.max_tool_iterations,
            brave_api_key=search_cfg.api_key or None,
            web_search_config=search_cfg,
            web_fetch_config=fetch_cfg,
            connector_config=connector_cfg,
            browser_config=browser_cfg,
            exec_config=exec_cfg,
            tool_policy_config=self.config.tools.policy,
            restrict_to_workspace=self.config.tools.restrict_to_workspace,
            allowed_models=profile.allowed_models,
            compression_trigger_ratio=profile.compression_trigger_ratio,
            compression_hysteresis_ratio=profile.compression_hysteresis_ratio,
            compression_cooldown_turns=profile.compression_cooldown_turns,
            thinking_model=profile.thinking_model or None,
            fallback_model=profile.fallback_model or None,
            cost_model=profile.cost_model or None,
            stability_model=profile.stability_model or None,
            intent_model_overrides=profile.intent_model_overrides,
            task_type_model_overrides=profile.task_type_model_overrides,
            agent_id=profile.agent_id,
            dev_profile=profile.dev_profile,
            trusted_local_only=profile.trusted_local_only,
        )

    def _build_provider_for_agent(self, profile: ResolvedAgentProfile) -> LLMProvider:
        if self.provider_factory is not None:
            return self.provider_factory(profile.model)
        if self.provider is None:
            raise ValueError("AgentPool requires either provider or provider_factory")
        return self.provider

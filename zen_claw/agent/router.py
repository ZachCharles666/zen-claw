"""Registry-aware agent routing for gateway and orchestration entry points."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from zen_claw.agent.pool import normalize_agent_id
from zen_claw.config.schema import Config


@dataclass(frozen=True)
class AgentRouteDecision:
    """Final agent selection for one inbound request."""

    agent_id: str
    reason: str
    matched_keyword: str = ""
    registered: bool = True


class AgentRouter:
    """Resolve the target agent profile for an inbound request."""

    def __init__(
        self,
        config: Config,
        *,
        resolve_bound_agent: Callable[[str, str, str], str | None] | None = None,
    ):
        self.config = config
        self.resolve_bound_agent = resolve_bound_agent

    def resolve(
        self,
        *,
        channel: str,
        chat_id: str,
        user_id: str,
        content: str,
        explicit_agent_id: str = "",
    ) -> AgentRouteDecision:
        explicit = self._normalize_optional_agent_id(explicit_agent_id)
        if explicit is not None:
            if explicit != "default" and explicit not in self.config.agents.profiles:
                return AgentRouteDecision(agent_id=explicit, reason="explicit", registered=False)
            if not self._is_profile_allowed_on_channel(explicit, channel):
                return AgentRouteDecision(agent_id="default", reason="explicit_denied", registered=True)
            return AgentRouteDecision(
                agent_id=explicit,
                reason="explicit",
                registered=explicit == "default" or explicit in self.config.agents.profiles,
            )

        bound = self._resolve_bound_agent(channel=channel, chat_id=chat_id, user_id=user_id)
        if bound:
            normalized = self._normalize_optional_agent_id(bound)
            if normalized is None:
                normalized = "default"
            if not self._is_profile_allowed_on_channel(normalized, channel):
                normalized = "default"
            return AgentRouteDecision(
                agent_id=normalized,
                reason="bound_route",
                registered=normalized == "default" or normalized in self.config.agents.profiles,
            )

        keyword_match = self._match_keyword(content, channel=channel)
        if keyword_match is not None:
            return keyword_match

        channel_profile = self._channel_profile_for(channel)
        if channel_profile:
            normalized = self._normalize_optional_agent_id(channel_profile)
            if normalized is None:
                normalized = "default"
            if not self._is_profile_allowed_on_channel(normalized, channel):
                normalized = "default"
            return AgentRouteDecision(
                agent_id=normalized,
                reason="channel_profile",
                registered=normalized == "default" or normalized in self.config.agents.profiles,
            )

        return AgentRouteDecision(agent_id="default", reason="fallback_default", registered=True)

    @staticmethod
    def _normalize_optional_agent_id(agent_id: str) -> str | None:
        raw = str(agent_id or "").strip()
        if not raw:
            return None
        return normalize_agent_id(raw)

    def _resolve_bound_agent(self, *, channel: str, chat_id: str, user_id: str) -> str | None:
        if self.resolve_bound_agent is None:
            return None
        return self.resolve_bound_agent(channel, chat_id, user_id)

    def _channel_profile_for(self, channel: str) -> str:
        channel_key = str(channel or "").strip().lower()
        if not channel_key:
            return ""
        channels_cfg = getattr(self.config, "channels", None)
        channel_cfg = getattr(channels_cfg, channel_key, None)
        if channel_cfg is None:
            return ""
        return str(getattr(channel_cfg, "agent_profile", "") or "").strip()

    def _match_keyword(self, content: str, *, channel: str) -> AgentRouteDecision | None:
        text = " ".join(str(content or "").strip().lower().split())
        if not text:
            return None

        best: tuple[int, str, str] | None = None
        for agent_id, profile in self.config.agents.profiles.items():
            if not self._is_profile_allowed_on_channel(agent_id, channel):
                continue
            for keyword in getattr(profile, "routing_keywords", []):
                if keyword and keyword in text:
                    candidate = (len(keyword), keyword, agent_id)
                    if best is None or candidate > best:
                        best = candidate
        if best is None:
            return None
        _length, keyword, agent_id = best
        return AgentRouteDecision(
            agent_id=normalize_agent_id(agent_id),
            reason="keyword",
            matched_keyword=keyword,
            registered=True,
        )

    def _is_profile_allowed_on_channel(self, agent_id: str, channel: str) -> bool:
        normalized = normalize_agent_id(agent_id)
        if normalized == "default":
            return True
        profile = self.config.agents.profiles.get(normalized)
        if profile is None:
            return False
        channel_key = str(channel or "").strip().lower()
        allowed_channels = {str(v).strip().lower() for v in getattr(profile, "allowed_channels", []) if str(v).strip()}
        if allowed_channels and channel_key not in allowed_channels:
            return False
        trusted_locals = {
            str(v).strip().lower()
            for v in (getattr(self.config.tools.policy, "trusted_local_channels", []) or [])
            if str(v).strip()
        }
        if bool(getattr(profile, "dev_profile", False)) or bool(getattr(profile, "trusted_local_only", False)):
            return channel_key in trusted_locals
        return True

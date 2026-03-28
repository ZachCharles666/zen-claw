"""Intent router façade for high-certainty pre-LLM utility requests."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from zen_claw.agent.intent_router_contracts import (
    ExchangeRateResolution,
    IntentRouteResult,
    IntentToolContract,
    RecoveryBlocker,
    RecoveryGuidance,
    RecoveryOutcome,
    RecoveryPlan,
    RecoveryStrategy,
    RetryPolicy,
    SourceFallbackResult,
)
from zen_claw.agent.intent_router_handlers import IntentRegistryEntry, IntentRouterHandlersMixin
from zen_claw.agent.intent_router_parsers import IntentRouterParsersMixin
from zen_claw.agent.intent_router_recovery import IntentRouterRecoveryMixin
from zen_claw.agent.intent_router_shared import IntentRouterSharedMixin
from zen_claw.agent.intent_router_specs import IntentRouterSpecsMixin


class IntentRouter(
    IntentRouterHandlersMixin,
    IntentRouterRecoveryMixin,
    IntentRouterSharedMixin,
    IntentRouterParsersMixin,
    IntentRouterSpecsMixin,
):
    """Handle a narrow set of deterministic, low-risk intents before LLM planning."""

    def __init__(self, *, allow_runtime_constrained_replan: bool = False) -> None:
        self.allow_runtime_constrained_replan = allow_runtime_constrained_replan


__all__ = [
    "ExchangeRateResolution",
    "IntentRegistryEntry",
    "IntentRouteResult",
    "IntentRouter",
    "IntentToolContract",
    "RecoveryBlocker",
    "RecoveryGuidance",
    "RecoveryOutcome",
    "RecoveryPlan",
    "RecoveryStrategy",
    "RetryPolicy",
    "SourceFallbackResult",
    "ZoneInfo",
]

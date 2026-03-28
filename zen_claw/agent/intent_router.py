"""Intent router façade for high-certainty pre-LLM utility requests.

Supports two routing layers:
1. **Declarative intents** — data-driven, single-tool, zero-boilerplate (checked first)
2. **Native mixin handlers** — complex multi-source intents with custom handler code
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from zen_claw.agent.tools.registry import ToolRegistry

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
from zen_claw.agent.intent_router_declarative import DeclarativeIntent, DeclarativeIntentEngine
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
    """Handle a narrow set of deterministic, low-risk intents before LLM planning.

    Routing order:
    1. Declarative intents (fast, data-driven)
    2. Native mixin handlers (complex, multi-source)
    """

    def __init__(self, *, allow_runtime_constrained_replan: bool = False) -> None:
        self.allow_runtime_constrained_replan = allow_runtime_constrained_replan
        self.declarative_engine = DeclarativeIntentEngine()
        self._load_builtin_declarative_intents()

    def _load_builtin_declarative_intents(self) -> None:
        """Load built-in declarative intent definitions."""
        try:
            from zen_claw.agent.intent_router_daily import DAILY_INTENTS

            self.declarative_engine.register_many(DAILY_INTENTS)
        except Exception:
            pass  # Daily tools may not be configured; skip silently

    async def route(
        self,
        content: str,
        *,
        tools: "ToolRegistry",
        trace_id: str,
    ) -> IntentRouteResult:
        """Route user intent through declarative engine first, then native handlers."""
        # Layer 1: Declarative intents (simple, single-tool)
        declarative_result = await self.declarative_engine.route(
            content, tools=tools, trace_id=trace_id
        )
        if declarative_result is not None:
            return declarative_result

        # Layer 2: Native mixin handlers (complex, multi-source)
        return await super().route(content, tools=tools, trace_id=trace_id)


__all__ = [
    "DeclarativeIntent",
    "DeclarativeIntentEngine",
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

"""Intent router façade for high-certainty pre-LLM utility requests.

Supports two routing layers:
1. **Declarative intents** — data-driven, single-tool, zero-boilerplate (checked first)
2. **Native mixin handlers** — complex multi-source intents with custom handler code
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from zen_claw.agent.tools.registry import ToolRegistry

from zen_claw.agent.intent_router_contracts import (
    ControlSignals,
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
from zen_claw.agent.intent_router_crystallized import CrystallizedIntentEngine
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
        self.crystallized_engine = CrystallizedIntentEngine()
        self.declarative_engine = DeclarativeIntentEngine()
        self._control_context: dict[str, Any] | None = None
        self._load_builtin_declarative_intents()

    def _load_builtin_declarative_intents(self) -> None:
        """Load built-in declarative intent definitions."""
        try:
            from zen_claw.agent.intent_router_daily import CRYSTALLIZED_DAILY_INTENTS, DAILY_INTENTS

            self.crystallized_engine.register_many(CRYSTALLIZED_DAILY_INTENTS)
            self.declarative_engine.register_many(DAILY_INTENTS)
        except Exception:
            pass  # Daily tools may not be configured; skip silently

    def set_control_context(self, control_context: dict[str, Any] | None) -> None:
        """Attach session-derived control context for the next routing attempt."""
        self._control_context = control_context if isinstance(control_context, dict) else None

    @staticmethod
    def _contains_correction_signal(content: str) -> bool:
        lowered = content.strip().lower()
        if not lowered:
            return False
        signals = (
            "不是",
            "不对",
            "错了",
            "搞错",
            "not that",
            "that's not right",
            "wrong",
        )
        return any(signal in lowered for signal in signals)

    def _should_delegate_correction_override(self, content: str) -> bool:
        if not self._contains_correction_signal(content):
            return False
        context = self._control_context if isinstance(self._control_context, dict) else {}
        last_rule = context.get("intent_router_last_rule")
        if not isinstance(last_rule, dict):
            return False
        route_status = str(last_rule.get("route_status") or "").strip().lower()
        source = str(last_rule.get("source") or "").strip().lower()
        return route_status in {"direct_success", "direct_failed"} and source in {
            "declarative",
            "native",
            "crystallized",
        }

    def _build_control_signals(self) -> ControlSignals:
        context = self._control_context if isinstance(self._control_context, dict) else {}
        history = context.get("intent_router_history")
        history_confidence: dict[str, float] = {}
        if isinstance(history, dict):
            for intent_name, stats in history.items():
                if not isinstance(stats, dict):
                    continue
                try:
                    success = max(int(stats.get("success", 0) or 0), 0)
                    failure = max(int(stats.get("failure", 0) or 0), 0)
                except (TypeError, ValueError):
                    continue
                total = success + failure
                history_confidence[str(intent_name)] = success / total if total > 0 else 1.0
        emotion_signal = context.get("emotion_signal")
        identity_signal = context.get("identity_signal")
        return ControlSignals(
            history_confidence=history_confidence,
            emotion_signal=emotion_signal if isinstance(emotion_signal, dict) else {},
            identity_signal=identity_signal if isinstance(identity_signal, dict) else {},
            extra={},
        )

    @staticmethod
    def _build_correction_delegate_result(content: str) -> IntentRouteResult:
        return IntentRouteResult(
            handled=True,
            content=content,
            route_status="needs_constrained_replan",
            diagnostic="gate1:correction_override",
            delegate_reason="correction_override",
            safety_valve_outcome="delegate",
        )

    async def route(
        self,
        content: str,
        *,
        tools: "ToolRegistry",
        trace_id: str,
    ) -> IntentRouteResult:
        """Route user intent through declarative engine first, then native handlers."""
        if self._should_delegate_correction_override(content):
            return self._build_correction_delegate_result(content)

        control_signals = self._build_control_signals()

        crystallized_match = self.crystallized_engine.match(
            content,
            control_context=self._control_context,
        )
        if crystallized_match is not None:
            crystallized_intent, route_candidate = crystallized_match
            target_intent = self.declarative_engine.get_intent(crystallized_intent.target_intent_name)
            if target_intent is not None:
                crystallized_result = await self.declarative_engine.route_via_candidate(
                    intent=target_intent,
                    content=content,
                    tools=tools,
                    trace_id=trace_id,
                    control_signals=control_signals,
                    route_candidate=route_candidate,
                )
                if crystallized_result is not None:
                    return crystallized_result

        # Layer 1: Declarative intents (simple, single-tool)
        declarative_result = await self.declarative_engine.route(
            content,
            tools=tools,
            trace_id=trace_id,
            control_signals=control_signals,
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

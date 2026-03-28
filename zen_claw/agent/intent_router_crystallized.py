"""Crystallized Gate 1 candidate source.

This module models "behavior solidification" as a first-class candidate source
that runs before declarative and native handlers but still feeds into the same
candidate / Safety Valve path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CrystallizedIntent:
    """A promoted high-confidence phrase-to-intent mapping."""

    name: str
    target_intent_name: str
    patterns: list[str]
    negative_patterns: list[str] = field(default_factory=list)
    raw_confidence: float = 0.97
    max_consecutive_failures: int = 2
    _compiled: list[re.Pattern[str]] | None = field(default=None, repr=False)
    _compiled_neg: list[re.Pattern[str]] | None = field(default=None, repr=False)

    def _ensure_compiled(self) -> None:
        if self._compiled is None:
            self._compiled = [re.compile(p, re.IGNORECASE) for p in self.patterns]
        if self._compiled_neg is None:
            self._compiled_neg = [re.compile(p, re.IGNORECASE) for p in self.negative_patterns]

    def match(self, content: str) -> bool:
        self._ensure_compiled()
        assert self._compiled is not None
        assert self._compiled_neg is not None
        if any(p.search(content) for p in self._compiled_neg):
            return False
        return any(p.search(content) for p in self._compiled)

    def build_route_candidate(self) -> dict[str, Any]:
        return {
            "match": self.target_intent_name,
            "source": "crystallized",
            "raw_confidence": self.raw_confidence,
            "crystallized_route": self.name,
        }


class CrystallizedIntentEngine:
    """Evaluate crystallized routes before other Gate 1 candidate sources."""

    def __init__(self) -> None:
        self._intents: list[CrystallizedIntent] = []

    def register(self, intent: CrystallizedIntent) -> None:
        self._intents.append(intent)

    def register_many(self, intents: list[CrystallizedIntent]) -> None:
        self._intents.extend(intents)

    def match(
        self,
        content: str,
        *,
        control_context: dict[str, Any] | None = None,
    ) -> tuple[CrystallizedIntent, dict[str, Any]] | None:
        context = control_context if isinstance(control_context, dict) else {}
        state_map = context.get("intent_router_crystallized_state")
        if not isinstance(state_map, dict):
            state_map = {}
        for intent in self._intents:
            if self._is_retired(intent, state_map.get(intent.name)):
                continue
            if intent.match(content):
                return intent, intent.build_route_candidate()
        return None

    @staticmethod
    def _is_retired(intent: CrystallizedIntent, state: Any) -> bool:
        if not isinstance(state, dict):
            return False
        try:
            failures = max(int(state.get("consecutive_failures", 0) or 0), 0)
        except (TypeError, ValueError):
            return False
        return failures >= intent.max_consecutive_failures

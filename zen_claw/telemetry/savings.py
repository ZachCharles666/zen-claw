"""Token savings report — B1.

Computes dollar savings achieved by routing low-complexity requests to a
cheaper ``cost_model`` instead of the configured ``base_model``.

Pricing table values are in USD per 1 000 *output* tokens; input tokens are
priced at 1/3 of the output rate by default (approximation that avoids
maintaining two separate tables).  The table can be overridden at runtime by
passing ``price_overrides``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zen_claw.telemetry.token_usage import TokenUsageRecord, TokenUsageTracker


# ── Built-in pricing table ─────────────────────────────────────────────────
# USD per 1 000 output tokens.  Input token price = output * INPUT_RATIO.

INPUT_RATIO: float = 0.33  # input tokens are ~1/3 the cost of output tokens

BUILTIN_PRICES: dict[str, float] = {
    # Anthropic
    "claude-opus-4-6": 15.0,
    "claude-opus-4": 15.0,
    "claude-opus-3-5": 15.0,
    "claude-sonnet-4-6": 3.0,
    "claude-sonnet-3-7": 3.0,
    "claude-sonnet-3-5": 3.0,
    "claude-haiku-4-5": 0.25,
    "claude-haiku-3-5": 0.25,
    "claude-haiku-3": 0.25,
    # OpenAI
    "gpt-4o": 10.0,
    "gpt-4o-mini": 0.6,
    "gpt-4-turbo": 30.0,
    "gpt-3.5-turbo": 0.5,
    # Google
    "gemini-pro": 0.5,
    "gemini-1.5-pro": 3.5,
    "gemini-1.5-flash": 0.075,
    "gemini-2.0-flash": 0.075,
    # Groq / open-weight
    "llama-3.3-70b": 0.59,
    "llama-3.1-8b": 0.05,
    "mistral-large": 2.0,
    "mistral-small": 0.1,
}


def _price_per_k(model: str, overrides: dict[str, float] | None = None) -> float:
    """Return output price per 1 000 tokens for *model*.

    Strips version suffixes like ``-20241022`` and tries prefix matching.
    Returns 0 if the model is unknown (so unknown models don't inflate savings).
    """
    table = dict(BUILTIN_PRICES)
    if overrides:
        table.update(overrides)
    m = str(model or "").strip().lower()
    if m in table:
        return table[m]
    # prefix match (e.g. "claude-sonnet-4-6-20251022" → "claude-sonnet-4-6")
    for key in sorted(table, key=len, reverse=True):
        if m.startswith(key):
            return table[key]
    return 0.0


def _token_cost_usd(model: str, input_tokens: int, output_tokens: int,
                    overrides: dict[str, float] | None = None) -> float:
    """Return estimated USD cost for a single LLM call."""
    price_out = _price_per_k(model, overrides)
    price_in = price_out * INPUT_RATIO
    return (output_tokens * price_out + input_tokens * price_in) / 1000.0


# ── Report dataclass ────────────────────────────────────────────────────────

@dataclass
class PeriodSavings:
    """Savings summary for a named time window."""

    period: str          # "today" | "week" | "month" | "all"
    actual_cost_usd: float = 0.0
    hypothetical_cost_usd: float = 0.0
    records: int = 0

    @property
    def saved_usd(self) -> float:
        return max(0.0, self.hypothetical_cost_usd - self.actual_cost_usd)

    @property
    def savings_pct(self) -> float:
        if self.hypothetical_cost_usd <= 0:
            return 0.0
        return self.saved_usd / self.hypothetical_cost_usd * 100.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "actual_cost_usd": round(self.actual_cost_usd, 6),
            "hypothetical_cost_usd": round(self.hypothetical_cost_usd, 6),
            "saved_usd": round(self.saved_usd, 6),
            "savings_pct": round(self.savings_pct, 2),
            "records": self.records,
        }


@dataclass
class SavingsReport:
    """Full savings report returned by :class:`SavingsCalculator`."""

    generated_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    periods: list[PeriodSavings] = field(default_factory=list)
    by_agent: dict[str, PeriodSavings] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at_ms": self.generated_at_ms,
            "periods": [p.to_dict() for p in self.periods],
            "by_agent": {k: v.to_dict() for k, v in self.by_agent.items()},
        }


# ── Calculator ──────────────────────────────────────────────────────────────

class SavingsCalculator:
    """Calculate token cost savings from ``cost_model`` routing.

    Parameters
    ----------
    tracker:
        Loaded :class:`~zen_claw.telemetry.token_usage.TokenUsageTracker`.
    base_model:
        The "full-price" model assumed if no cheaper routing had been used.
        Defaults to ``claude-opus-4-6``.
    price_overrides:
        Optional per-model price overrides (USD / 1K output tokens).
    """

    def __init__(
        self,
        tracker: TokenUsageTracker,
        *,
        base_model: str = "claude-opus-4-6",
        price_overrides: dict[str, float] | None = None,
    ) -> None:
        self._tracker = tracker
        self._base_model = base_model
        self._overrides = price_overrides

    # ── public ────────────────────────────────────────────────────────────

    def report(self, *, since_ms: int | None = None) -> SavingsReport:
        """Compute savings over all time, today, this week, and this month."""
        records = self._tracker.read_all(limit=100_000)
        if since_ms is not None:
            records = [r for r in records if r.at_ms >= since_ms]

        now_ms = int(time.time() * 1000)
        today_start = self._day_start_ms(now_ms)
        week_start = today_start - 6 * 86_400_000
        month_start = today_start - 29 * 86_400_000

        all_s = self._sum_records(records, "all")
        today_s = self._sum_records([r for r in records if r.at_ms >= today_start], "today")
        week_s = self._sum_records([r for r in records if r.at_ms >= week_start], "week")
        month_s = self._sum_records([r for r in records if r.at_ms >= month_start], "month")

        # Per-agent breakdown (all time)
        by_agent: dict[str, list[TokenUsageRecord]] = {}
        for rec in records:
            by_agent.setdefault(rec.agent_id, []).append(rec)
        agent_savings = {
            agent_id: self._sum_records(recs, agent_id)
            for agent_id, recs in by_agent.items()
        }

        return SavingsReport(
            generated_at_ms=now_ms,
            periods=[all_s, today_s, week_s, month_s],
            by_agent=agent_savings,
        )

    # ── private ───────────────────────────────────────────────────────────

    def _sum_records(
        self, records: list[TokenUsageRecord], label: str
    ) -> PeriodSavings:
        s = PeriodSavings(period=label)
        for rec in records:
            actual = _token_cost_usd(
                rec.model, rec.input_tokens, rec.output_tokens, self._overrides
            )
            hypothetical = _token_cost_usd(
                self._base_model, rec.input_tokens, rec.output_tokens, self._overrides
            )
            s.actual_cost_usd += actual
            s.hypothetical_cost_usd += hypothetical
            s.records += 1
        return s

    @staticmethod
    def _day_start_ms(now_ms: int) -> int:
        """Return the start of the current UTC day in milliseconds."""
        day_sec = (now_ms // 1000) - ((now_ms // 1000) % 86400)
        return day_sec * 1000

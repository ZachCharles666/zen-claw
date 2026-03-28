"""Tests for B1 — Token savings visualization report."""

from __future__ import annotations

from pathlib import Path

from zen_claw.telemetry.savings import (
    SavingsCalculator,
    _price_per_k,
    _token_cost_usd,
)
from zen_claw.telemetry.token_usage import TokenUsageTracker

# ── pricing helpers ───────────────────────────────────────────────────────────

def test_price_per_k_known_model():
    price = _price_per_k("claude-opus-4-6")
    assert price == 15.0


def test_price_per_k_unknown_model_returns_zero():
    assert _price_per_k("unknown-model-xyz") == 0.0


def test_price_per_k_prefix_match():
    # "claude-sonnet-4-6-20251022" should match "claude-sonnet-4-6"
    price = _price_per_k("claude-sonnet-4-6-20251022")
    assert price == 3.0


def test_price_per_k_override():
    override = {"custom-model": 42.0}
    assert _price_per_k("custom-model", overrides=override) == 42.0


def test_token_cost_usd_cheap_model_less_than_expensive():
    cost_cheap = _token_cost_usd("claude-haiku-4-5", 1000, 500)
    cost_expensive = _token_cost_usd("claude-opus-4-6", 1000, 500)
    assert cost_cheap < cost_expensive


def test_token_cost_usd_unknown_model_is_zero():
    assert _token_cost_usd("mystery-model", 1000, 500) == 0.0


# ── SavingsCalculator ─────────────────────────────────────────────────────────

def _make_tracker_with_records(tmp_path: Path, model: str, n: int = 3) -> TokenUsageTracker:
    tracker = TokenUsageTracker(tmp_path)
    for i in range(n):
        tracker.record(
            agent_id="bot1",
            model=model,
            input_tokens=100 * (i + 1),
            output_tokens=50 * (i + 1),
        )
    return tracker


def test_savings_report_periods_present(tmp_path: Path):
    tracker = _make_tracker_with_records(tmp_path, "claude-haiku-4-5", n=2)
    calc = SavingsCalculator(tracker, base_model="claude-opus-4-6")
    report = calc.report()
    period_names = {p["period"] for p in report.to_dict()["periods"]}
    assert {"all", "today", "week", "month"} == period_names


def test_savings_cheap_model_shows_positive_savings(tmp_path: Path):
    tracker = _make_tracker_with_records(tmp_path, "claude-haiku-4-5", n=5)
    calc = SavingsCalculator(tracker, base_model="claude-opus-4-6")
    report = calc.report()
    # all-time period
    all_period = next(p for p in report.to_dict()["periods"] if p["period"] == "all")
    assert all_period["saved_usd"] > 0
    assert all_period["savings_pct"] > 0


def test_savings_same_model_zero_savings(tmp_path: Path):
    tracker = _make_tracker_with_records(tmp_path, "claude-opus-4-6", n=3)
    calc = SavingsCalculator(tracker, base_model="claude-opus-4-6")
    report = calc.report()
    all_period = next(p for p in report.to_dict()["periods"] if p["period"] == "all")
    assert all_period["saved_usd"] == 0.0


def test_savings_by_agent_breakdown(tmp_path: Path):
    tracker = TokenUsageTracker(tmp_path)
    tracker.record(agent_id="agent_a", model="claude-haiku-4-5", input_tokens=100, output_tokens=50)
    tracker.record(agent_id="agent_b", model="claude-haiku-4-5", input_tokens=200, output_tokens=100)

    calc = SavingsCalculator(tracker, base_model="claude-opus-4-6")
    report = calc.report()
    by_agent = report.to_dict()["by_agent"]
    assert "agent_a" in by_agent
    assert "agent_b" in by_agent
    assert by_agent["agent_a"]["records"] == 1
    assert by_agent["agent_b"]["records"] == 1

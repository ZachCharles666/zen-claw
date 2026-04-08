"""Evaluation harness for IntentRouter Gate 1 routing accuracy.

Run with:
    pytest tests/eval/intent_router_eval.py -m eval -v

Requires:
    pytest.ini or pyproject.toml to define the `eval` mark.

Metrics computed:
    - Gate1 Precision@route: correct Gate 1 routes / total Gate 1 routes
    - False positive rate: non-routable requests incorrectly routed
    - Safety valve delegation rate: compound/ambiguous requests correctly delegated
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

CORPUS_PATH = Path(__file__).parent / "fixtures" / "intent_corpus.jsonl"


def _load_corpus() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with CORPUS_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _build_router_and_registry():
    """Build an IntentRouter with a minimal mock ToolRegistry."""
    from zen_claw.agent.intent_router import IntentRouter
    from zen_claw.agent.tools.result import ToolErrorKind, ToolResult

    mock_registry = MagicMock()
    mock_registry.has.return_value = True
    mock_registry.list_tools.return_value = []
    mock_registry._request_context = {}
    mock_registry.get_policy_snapshot_hash.return_value = ""
    # Use AsyncMock for async tool execution — return a generic failure so
    # native handlers don't crash but still complete routing logic.
    mock_registry.execute = AsyncMock(
        return_value=ToolResult.failure(ToolErrorKind.RETRYABLE, "eval_mock", code="eval_mock")
    )
    router = IntentRouter()
    return router, mock_registry


def _route(router, registry, content: str, control_context: dict | None = None) -> Any:
    """Run router.route() synchronously for eval purposes."""
    router.set_control_context(control_context or {})
    return asyncio.run(router.route(content, tools=registry, trace_id="eval"))


# ── pytest fixtures ───────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def corpus() -> list[dict[str, Any]]:
    return _load_corpus()


@pytest.fixture(scope="module")
def router_and_registry():
    return _build_router_and_registry()


# ── offline evaluation tests ──────────────────────────────────────────────────


@pytest.mark.eval
class TestGate1Routing:
    """Gate 1 routing accuracy tests — no LLM calls required."""

    def test_corpus_loads(self, corpus: list[dict[str, Any]]) -> None:
        assert len(corpus) >= 30, f"Corpus too small: {len(corpus)} entries"

    def test_gate1_route_precision(
        self, corpus: list[dict[str, Any]], router_and_registry
    ) -> None:
        """Correct Gate 1 routes / total Gate 1 route attempts."""
        router, registry = router_and_registry
        gate1_cases = [r for r in corpus if r.get("label") == "gate1_route"]
        correct = 0
        total = 0
        failures: list[str] = []

        for case in gate1_cases:
            result = _route(router, registry, case["content"])
            total += 1
            expected_intent = case.get("expected_gate1_intent")

            # In mock mode tools return failure → route becomes direct_failed.
            # Both direct_success and direct_failed count as "correctly routed"
            # since we are testing pattern recognition, not live tool execution.
            routed = result.route_status in {"direct_success", "direct_failed"}

            if routed:
                if expected_intent is None or result.intent_name == expected_intent:
                    correct += 1
                else:
                    failures.append(
                        f"[{case['id']}] intent mismatch: got={result.intent_name!r} want={expected_intent!r}"
                    )
            else:
                failures.append(
                    f"[{case['id']}] not routed: got={result.route_status!r} content={case['content']!r}"
                )

        precision = correct / total if total > 0 else 0.0
        print(f"\nGate1 Precision: {precision:.1%} ({correct}/{total})")
        if failures:
            print("Failures:\n  " + "\n  ".join(failures))

        # Baseline (2026-04-08): 76.2%. Threshold set at 70% to track regressions
        # while leaving room for incremental improvement via pattern expansion.
        assert precision >= 0.70, (
            f"Gate1 precision {precision:.1%} below 70% threshold.\nFailures:\n"
            + "\n".join(failures)
        )

    def test_gate1_negative_miss_rate(
        self, corpus: list[dict[str, Any]], router_and_registry
    ) -> None:
        """Negative examples (non-routable) should NOT be routed by Gate 1."""
        router, registry = router_and_registry
        negative_cases = [r for r in corpus if r.get("label") == "negative"]
        false_positives: list[str] = []

        for case in negative_cases:
            result = _route(router, registry, case["content"])
            if result.route_status in {"direct_success", "direct_failed"}:
                false_positives.append(
                    f"[{case['id']}] falsely routed as {result.intent_name!r}: {case['content']!r}"
                )

        false_positive_rate = len(false_positives) / max(1, len(negative_cases))
        print(
            f"\nGate1 False Positive Rate: {false_positive_rate:.1%} "
            f"({len(false_positives)}/{len(negative_cases)})"
        )
        if false_positives:
            print("False positives:\n  " + "\n  ".join(false_positives))

        assert false_positive_rate <= 0.20, (
            f"Gate1 false positive rate {false_positive_rate:.1%} exceeds 20% threshold."
        )

    def test_safety_valve_fires_on_delegate_cases(
        self, corpus: list[dict[str, Any]], router_and_registry
    ) -> None:
        """Safety valve cases should delegate, not route directly."""
        router, registry = router_and_registry
        sv_cases = [r for r in corpus if r.get("label") == "safety_valve_delegate"]
        missed: list[str] = []

        for case in sv_cases:
            result = _route(router, registry, case["content"])
            if result.route_status not in {"needs_constrained_replan", "miss"}:
                missed.append(
                    f"[{case['id']}] expected delegation, got {result.route_status!r}: {case['content']!r}"
                )

        miss_rate = len(missed) / max(1, len(sv_cases))
        print(
            f"\nSafety Valve Delegation Rate: {(1 - miss_rate):.1%} "
            f"({len(sv_cases) - len(missed)}/{len(sv_cases)})"
        )
        if missed:
            print("Not delegated:\n  " + "\n  ".join(missed))

        assert miss_rate <= 0.30, (
            f"Too many safety-valve cases were routed directly: {miss_rate:.1%} miss rate."
        )


# ── full metrics report ───────────────────────────────────────────────────────


@pytest.mark.eval
def test_print_full_metrics_report(
    corpus: list[dict[str, Any]], router_and_registry
) -> None:
    """Print a full routing accuracy report for the corpus."""
    router, registry = router_and_registry
    results: dict[str, dict[str, int]] = {
        "gate1_route": {"correct": 0, "total": 0},
        "safety_valve_delegate": {"correct": 0, "total": 0},
        "negative": {"correct": 0, "total": 0},
        "penalty_test": {"delegated": 0, "total": 0},
    }

    for case in corpus:
        label = case.get("label", "")
        content = case["content"]
        result = _route(router, registry, content)

        if label == "gate1_route":
            results["gate1_route"]["total"] += 1
            expected_intent = case.get("expected_gate1_intent")
            routed = result.route_status in {"direct_success", "direct_failed"}
            if routed and (expected_intent is None or result.intent_name == expected_intent):
                results["gate1_route"]["correct"] += 1

        elif label == "safety_valve_delegate":
            results["safety_valve_delegate"]["total"] += 1
            if result.route_status in {"needs_constrained_replan", "miss"}:
                results["safety_valve_delegate"]["correct"] += 1

        elif label == "negative":
            results["negative"]["total"] += 1
            if result.route_status not in {"direct_success", "direct_failed"}:
                results["negative"]["correct"] += 1

        elif label == "penalty_test":
            results["penalty_test"]["total"] += 1
            if result.route_status == "needs_constrained_replan":
                results["penalty_test"]["delegated"] += 1

    print("\n" + "=" * 60)
    print("INTENT ROUTER EVALUATION REPORT")
    print("=" * 60)
    for category, counts in results.items():
        total = counts.get("total", 0)
        correct = counts.get("correct", counts.get("delegated", 0))
        rate = correct / total if total > 0 else 0.0
        print(f"  {category:<30} {rate:.1%} ({correct}/{total})")
    print("=" * 60)

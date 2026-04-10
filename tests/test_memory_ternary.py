"""Unit tests for TernaryRecallStrategy and supporting types."""

from zen_claw.agent.memory_recall import KeywordRecallStrategy, NoopRecallStrategy
from zen_claw.agent.memory_ternary import (
    TernaryConfig,
    TernaryRecallStrategy,
    TernaryResult,
    TernaryTier,
    TriternaryRecallStrategy,
)

# ── helpers ───────────────────────────────────────────────────────────────────


class _FixedScorer:
    """Scorer that returns a predefined score map for testing."""

    def __init__(self, scores: dict[str, float]):
        self._scores = scores

    def score(self, query: str, candidate: str) -> float:
        return self._scores.get(candidate, 0.0)

    def bulk_score(self, query: str, candidates: list[str]) -> list[tuple[float, str]]:
        result = []
        for c in candidates:
            s = self._scores.get(c, 0.0)
            if s > 0:
                result.append((s, c))
        return sorted(result, key=lambda x: x[0], reverse=True)


# ── TernaryConfig ─────────────────────────────────────────────────────────────


def test_ternary_config_defaults() -> None:
    cfg = TernaryConfig()
    assert cfg.reject_threshold == 0.2
    assert cfg.accept_threshold == 0.6
    assert cfg.max_uncertain_candidates == 10
    assert cfg.secondary_accept_threshold == 0.6  # defaults to accept_threshold


def test_ternary_config_custom() -> None:
    cfg = TernaryConfig(reject_threshold=0.1, accept_threshold=0.7, max_uncertain_candidates=5)
    assert cfg.reject_threshold == 0.1
    assert cfg.accept_threshold == 0.7
    assert cfg.max_uncertain_candidates == 5
    assert cfg.secondary_accept_threshold == 0.7


def test_ternary_config_explicit_secondary_threshold() -> None:
    cfg = TernaryConfig(reject_threshold=0.1, accept_threshold=0.7, secondary_accept_threshold=0.5)
    assert cfg.secondary_accept_threshold == 0.5


def test_ternary_config_invalid_thresholds() -> None:
    import pytest

    with pytest.raises(ValueError, match="reject_threshold"):
        TernaryConfig(reject_threshold=0.7, accept_threshold=0.5)

    with pytest.raises(ValueError, match="reject_threshold"):
        TernaryConfig(reject_threshold=0.6, accept_threshold=0.6)


# ── TernaryTier ───────────────────────────────────────────────────────────────


def test_ternary_tier_values() -> None:
    assert TernaryTier.REJECT == -1
    assert TernaryTier.UNCERTAIN == 0
    assert TernaryTier.ACCEPT == 1


# ── TernaryResult ─────────────────────────────────────────────────────────────


def test_ternary_result_all_promoted_sorted() -> None:
    result = TernaryResult(
        accepted=[(0.8, "a"), (0.9, "b")],
        uncertain=[],
        rejected=[],
    )
    promoted = result.all_promoted
    assert promoted[0] == (0.9, "b")
    assert promoted[1] == (0.8, "a")


def test_ternary_result_empty() -> None:
    result = TernaryResult()
    assert result.all_promoted == []


# ── TernaryRecallStrategy.classify() ─────────────────────────────────────────


def test_classify_accept_at_threshold() -> None:
    strategy = TernaryRecallStrategy(primary=NoopRecallStrategy())
    assert strategy.classify(0.6) == TernaryTier.ACCEPT
    assert strategy.classify(0.9) == TernaryTier.ACCEPT
    assert strategy.classify(1.0) == TernaryTier.ACCEPT


def test_classify_reject_at_threshold() -> None:
    strategy = TernaryRecallStrategy(primary=NoopRecallStrategy())
    assert strategy.classify(0.2) == TernaryTier.REJECT
    assert strategy.classify(0.0) == TernaryTier.REJECT


def test_classify_uncertain_middle_band() -> None:
    strategy = TernaryRecallStrategy(primary=NoopRecallStrategy())
    assert strategy.classify(0.3) == TernaryTier.UNCERTAIN
    assert strategy.classify(0.5) == TernaryTier.UNCERTAIN
    assert strategy.classify(0.21) == TernaryTier.UNCERTAIN
    assert strategy.classify(0.59) == TernaryTier.UNCERTAIN


def test_classify_custom_thresholds() -> None:
    cfg = TernaryConfig(reject_threshold=0.3, accept_threshold=0.8)
    strategy = TernaryRecallStrategy(primary=NoopRecallStrategy(), config=cfg)
    assert strategy.classify(0.8) == TernaryTier.ACCEPT
    assert strategy.classify(0.5) == TernaryTier.UNCERTAIN
    assert strategy.classify(0.3) == TernaryTier.REJECT


# ── TernaryRecallStrategy.score() — backward compat ──────────────────────────


def test_score_delegates_to_primary() -> None:
    primary = _FixedScorer({"hello world memory": 0.75})
    strategy = TernaryRecallStrategy(primary=primary)
    assert strategy.score("query", "hello world memory") == 0.75
    assert strategy.score("query", "missing") == 0.0


# ── TernaryRecallStrategy.ternary_score() ────────────────────────────────────


def test_ternary_score_buckets_correctly() -> None:
    primary = _FixedScorer({"accept": 0.8, "uncertain": 0.4, "reject": 0.1})
    strategy = TernaryRecallStrategy(primary=primary)

    result = strategy.ternary_score("q", ["accept", "uncertain", "reject"])

    assert len(result.accepted) == 1
    assert result.accepted[0][1] == "accept"
    assert len(result.uncertain) == 1
    assert result.uncertain[0][1] == "uncertain"
    assert len(result.rejected) == 1
    assert result.rejected[0][1] == "reject"


def test_ternary_score_zero_scored_candidates_go_to_reject() -> None:
    """Candidates not returned by bulk_score get score 0.0 → REJECT."""
    primary = _FixedScorer({"only_this": 0.9})
    strategy = TernaryRecallStrategy(primary=primary)

    result = strategy.ternary_score("q", ["only_this", "missing_candidate"])

    assert any(t == "only_this" for _, t in result.accepted)
    assert any(t == "missing_candidate" for _, t in result.rejected)


def test_ternary_score_all_accepted() -> None:
    primary = _FixedScorer({"a": 0.7, "b": 0.8, "c": 0.9})
    strategy = TernaryRecallStrategy(primary=primary)
    result = strategy.ternary_score("q", ["a", "b", "c"])
    assert len(result.accepted) == 3
    assert result.uncertain == []
    assert result.rejected == []


def test_ternary_score_all_rejected() -> None:
    strategy = TernaryRecallStrategy(primary=NoopRecallStrategy())
    result = strategy.ternary_score("q", ["x", "y", "z"])
    assert result.accepted == []
    assert result.uncertain == []
    assert len(result.rejected) == 3


def test_ternary_score_empty_candidates() -> None:
    strategy = TernaryRecallStrategy(primary=NoopRecallStrategy())
    result = strategy.ternary_score("q", [])
    assert result.accepted == []
    assert result.uncertain == []
    assert result.rejected == []


def test_ternary_score_caps_uncertain_candidates() -> None:
    """Uncertain candidates beyond max_uncertain_candidates spill into rejected."""
    scores = {f"item_{i}": 0.4 for i in range(15)}  # all in uncertain band
    primary = _FixedScorer(scores)
    cfg = TernaryConfig(max_uncertain_candidates=5)
    strategy = TernaryRecallStrategy(primary=primary, config=cfg)

    result = strategy.ternary_score("q", list(scores.keys()))

    assert len(result.uncertain) == 5
    assert len(result.rejected) == 10  # 15 - 5 capped into rejected


# ── TernaryRecallStrategy.resolve_uncertain() ─────────────────────────────────


def test_resolve_uncertain_promotes_above_secondary_threshold() -> None:
    primary = _FixedScorer({})
    secondary = _FixedScorer({"will_promote": 0.7, "will_demote": 0.1})
    strategy = TernaryRecallStrategy(primary=primary, secondary=secondary)

    uncertain = [(0.4, "will_promote"), (0.35, "will_demote")]
    promoted, demoted = strategy.resolve_uncertain("q", uncertain)

    assert len(promoted) == 1
    assert promoted[0][1] == "will_promote"
    assert len(demoted) == 1
    assert demoted[0][1] == "will_demote"


def test_resolve_uncertain_empty_input() -> None:
    strategy = TernaryRecallStrategy(primary=NoopRecallStrategy())
    promoted, demoted = strategy.resolve_uncertain("q", [])
    assert promoted == []
    assert demoted == []


def test_resolve_uncertain_promoted_score_is_max_of_primary_secondary() -> None:
    primary = _FixedScorer({})
    secondary = _FixedScorer({"item": 0.8})
    strategy = TernaryRecallStrategy(primary=primary, secondary=secondary)

    uncertain = [(0.45, "item")]
    promoted, _ = strategy.resolve_uncertain("q", uncertain)

    # final score = max(primary=0.45, secondary=0.8)
    assert promoted[0][0] == 0.8


def test_resolve_uncertain_uses_custom_secondary_threshold() -> None:
    cfg = TernaryConfig(reject_threshold=0.1, accept_threshold=0.7, secondary_accept_threshold=0.5)
    primary = _FixedScorer({})
    secondary = _FixedScorer({"borderline": 0.55})
    strategy = TernaryRecallStrategy(primary=primary, config=cfg, secondary=secondary)

    uncertain = [(0.3, "borderline")]
    promoted, demoted = strategy.resolve_uncertain("q", uncertain)

    # secondary score 0.55 >= secondary_accept_threshold 0.5 → promoted
    assert len(promoted) == 1
    assert demoted == []


# ── TernaryRecallStrategy.ternary_recall() full pipeline ─────────────────────


def test_ternary_recall_full_pipeline() -> None:
    primary = _FixedScorer({"high": 0.9, "mid": 0.4, "low": 0.05})
    secondary = _FixedScorer({"mid": 0.7})  # will promote "mid"
    strategy = TernaryRecallStrategy(primary=primary, secondary=secondary)

    result = strategy.ternary_recall("q", ["high", "mid", "low"])

    texts = [t for _, t in result]
    assert "high" in texts
    assert "mid" in texts   # promoted from uncertain
    assert "low" not in texts  # rejected


def test_ternary_recall_sorted_descending() -> None:
    primary = _FixedScorer({"a": 0.7, "b": 0.9, "c": 0.65})
    strategy = TernaryRecallStrategy(primary=primary)

    result = strategy.ternary_recall("q", ["a", "b", "c"])

    scores = [s for s, _ in result]
    assert scores == sorted(scores, reverse=True)


# ── bulk_score fallback (no bulk_score on primary) ───────────────────────────


def test_bulk_score_fallback_when_primary_has_no_bulk_score() -> None:
    """TernaryRecallStrategy falls back to per-item score() when primary lacks bulk_score."""

    class _PerItemOnly:
        def score(self, query: str, candidate: str) -> float:
            return 0.8 if candidate == "match" else 0.0

    strategy = TernaryRecallStrategy(primary=_PerItemOnly())
    result = strategy.bulk_score("q", ["match", "no_match"])

    assert len(result) == 1
    assert result[0][1] == "match"
    assert result[0][0] == 0.8


# ── Integration: with real KeywordRecallStrategy ──────────────────────────────


def test_with_keyword_primary_and_secondary() -> None:
    primary = KeywordRecallStrategy()
    secondary = KeywordRecallStrategy()
    strategy = TernaryRecallStrategy(primary=primary, secondary=secondary)

    candidates = [
        "user prefers dark mode in settings",
        "the weather today is sunny",
        "python programming language syntax",
    ]
    result = strategy.ternary_recall("user settings preferences", candidates)

    # The settings-related memory should score highest
    texts = [t for _, t in result]
    if texts:
        assert texts[0] == "user prefers dark mode in settings"


# ── TriternaryRecallStrategy ──────────────────────────────────────────────────


class _FixedTriScorer:
    """Deterministic scorer for TriternaryRecallStrategy tests."""

    def __init__(self, scores: dict[str, float]):
        self._scores = scores
        self.calls: list[tuple[str, str]] = []

    def score(self, query: str, candidate: str) -> float:
        self.calls.append((query, candidate))
        return self._scores.get(candidate, 0.0)


def test_triternary_score_accept_tier() -> None:
    primary = _FixedTriScorer({"a": 0.8})
    strategy = TriternaryRecallStrategy(primary=primary)
    assert strategy.score("q", "a") == 1.0


def test_triternary_score_reject_tier() -> None:
    primary = _FixedTriScorer({"a": 0.1})
    strategy = TriternaryRecallStrategy(primary=primary)
    assert strategy.score("q", "a") == -1.0


def test_triternary_score_uncertain_tier() -> None:
    primary = _FixedTriScorer({"a": 0.4})
    strategy = TriternaryRecallStrategy(primary=primary)
    assert strategy.score("q", "a") == 0.0


def test_triternary_score_boundary_accept() -> None:
    """Score exactly at accept_threshold should return ACCEPT."""
    primary = _FixedTriScorer({"a": 0.6})
    strategy = TriternaryRecallStrategy(primary=primary)
    assert strategy.score("q", "a") == 1.0


def test_triternary_score_boundary_reject() -> None:
    """Score exactly at reject_threshold should return UNCERTAIN, not REJECT."""
    primary = _FixedTriScorer({"a": 0.2})
    strategy = TriternaryRecallStrategy(primary=primary, config=TernaryConfig(reject_threshold=0.2))
    # raw >= reject_threshold → not < reject_threshold → UNCERTAIN
    assert strategy.score("q", "a") == 0.0


def test_triternary_custom_config() -> None:
    cfg = TernaryConfig(reject_threshold=0.1, accept_threshold=0.5)
    primary = _FixedTriScorer({"a": 0.5, "b": 0.3, "c": 0.05})
    strategy = TriternaryRecallStrategy(primary=primary, config=cfg)
    assert strategy.score("q", "a") == 1.0   # >= 0.5
    assert strategy.score("q", "b") == 0.0   # 0.1 <= b < 0.5
    assert strategy.score("q", "c") == -1.0  # < 0.1


def test_triternary_recall_returns_only_accepted() -> None:
    primary = _FixedTriScorer({"accept_me": 0.9, "maybe": 0.4, "reject_me": 0.05})
    strategy = TriternaryRecallStrategy(primary=primary)
    result = strategy.recall("q", ["accept_me", "maybe", "reject_me"])
    assert result == ["accept_me"]
    assert "maybe" not in result
    assert "reject_me" not in result


def test_triternary_recall_respects_max_results() -> None:
    scores = {f"item_{i}": 0.9 for i in range(20)}
    primary = _FixedTriScorer(scores)
    strategy = TriternaryRecallStrategy(primary=primary)
    result = strategy.recall("q", list(scores.keys()), max_results=5)
    assert len(result) == 5


def test_triternary_recall_empty_candidates() -> None:
    primary = _FixedTriScorer({})
    strategy = TriternaryRecallStrategy(primary=primary)
    assert strategy.recall("q", []) == []


def test_triternary_primary_score_is_called() -> None:
    """Verify that the primary scorer's score() is invoked for each candidate."""
    primary = _FixedTriScorer({"x": 0.7})
    strategy = TriternaryRecallStrategy(primary=primary)
    strategy.score("query", "x")
    assert ("query", "x") in primary.calls


def test_triternary_default_config_is_ternary_config_defaults() -> None:
    primary = _FixedTriScorer({})
    strategy = TriternaryRecallStrategy(primary=primary)
    assert strategy._config.reject_threshold == 0.2
    assert strategy._config.accept_threshold == 0.6

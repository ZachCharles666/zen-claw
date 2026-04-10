"""Ternary memory recall: three-tier classification for memory retrieval.

Implements b1.58 ternary logic {-1, 0, +1} on top of existing float-based
MemoryRecallStrategy scorers.  Candidates are classified into Accept (+1),
Uncertain (0), or Reject (-1) based on configurable thresholds.  Uncertain
candidates undergo a secondary scoring pass for promotion or rejection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

from zen_claw.agent.memory_recall import KeywordRecallStrategy, MemoryRecallStrategy


class TernaryTier(IntEnum):
    """Three-tier classification matching b1.58 ternary values."""

    REJECT = -1
    UNCERTAIN = 0
    ACCEPT = 1


@dataclass
class TernaryConfig:
    """Thresholds and limits for ternary classification."""

    reject_threshold: float = 0.2
    accept_threshold: float = 0.6
    max_uncertain_candidates: int = 10
    secondary_accept_threshold: float | None = None  # defaults to accept_threshold

    def __post_init__(self) -> None:
        if self.reject_threshold >= self.accept_threshold:
            raise ValueError(
                f"reject_threshold ({self.reject_threshold}) must be less than "
                f"accept_threshold ({self.accept_threshold})"
            )
        if self.secondary_accept_threshold is None:
            self.secondary_accept_threshold = self.accept_threshold


@dataclass
class TernaryResult:
    """Outcome of a ternary scoring pass."""

    accepted: list[tuple[float, str]] = field(default_factory=list)
    uncertain: list[tuple[float, str]] = field(default_factory=list)
    rejected: list[tuple[float, str]] = field(default_factory=list)

    @property
    def all_promoted(self) -> list[tuple[float, str]]:
        """Return accepted candidates sorted by score descending."""
        return sorted(self.accepted, key=lambda x: x[0], reverse=True)


class TernaryRecallStrategy(MemoryRecallStrategy):
    """Three-tier memory recall: primary scoring → classification → secondary resolution.

    Wraps any existing ``MemoryRecallStrategy`` as the primary scorer.
    Candidates scoring above ``accept_threshold`` are immediately accepted;
    those below ``reject_threshold`` are immediately discarded; the middle
    band is re-scored by a lightweight secondary scorer for promotion.
    """

    def __init__(
        self,
        primary: MemoryRecallStrategy,
        config: TernaryConfig | None = None,
        secondary: MemoryRecallStrategy | None = None,
    ) -> None:
        self._primary = primary
        self._config = config or TernaryConfig()
        self._secondary = secondary or KeywordRecallStrategy()

    # ── MemoryRecallStrategy interface (backward compat) ──────────────

    def score(self, query: str, candidate: str) -> float:
        """Delegate to primary scorer — preserves float interface."""
        return self._primary.score(query, candidate)

    # ── Bulk scoring (duck-type compat with Rag/Sqlite strategies) ────

    def bulk_score(self, query: str, candidates: list[str]) -> list[tuple[float, str]]:
        """Delegate bulk scoring to primary if available, else fall back to per-item."""
        return self._bulk_score_with(self._primary, query, candidates)

    # ── Ternary-specific API ──────────────────────────────────────────

    def classify(self, score: float) -> TernaryTier:
        """Map a float score to a ternary tier."""
        if score >= self._config.accept_threshold:
            return TernaryTier.ACCEPT
        if score <= self._config.reject_threshold:
            return TernaryTier.REJECT
        return TernaryTier.UNCERTAIN

    def ternary_score(
        self, query: str, candidates: list[str]
    ) -> TernaryResult:
        """Score all candidates and classify into three tiers.

        Steps:
        1. Bulk-score via primary scorer.
        2. Classify each scored candidate.
        3. Candidates with zero score that weren't returned by bulk_score
           are classified as REJECT.
        """
        scored = self._bulk_score_with(self._primary, query, candidates)
        scored_map: dict[str, float] = {text: s for s, text in scored}

        result = TernaryResult()
        for candidate in candidates:
            s = scored_map.get(candidate, 0.0)
            tier = self.classify(s)
            pair = (s, candidate)
            if tier == TernaryTier.ACCEPT:
                result.accepted.append(pair)
            elif tier == TernaryTier.UNCERTAIN:
                result.uncertain.append(pair)
            else:
                result.rejected.append(pair)

        # Cap uncertain candidates to avoid expensive secondary pass
        if len(result.uncertain) > self._config.max_uncertain_candidates:
            # Keep top-scoring uncertain candidates
            result.uncertain.sort(key=lambda x: x[0], reverse=True)
            overflow = result.uncertain[self._config.max_uncertain_candidates :]
            result.uncertain = result.uncertain[: self._config.max_uncertain_candidates]
            result.rejected.extend(overflow)

        return result

    def resolve_uncertain(
        self, query: str, uncertain: list[tuple[float, str]]
    ) -> tuple[list[tuple[float, str]], list[tuple[float, str]]]:
        """Re-score uncertain candidates with secondary scorer.

        Returns:
            (promoted, demoted) — promoted candidates exceeded the secondary
            accept threshold; demoted ones did not.
        """
        if not uncertain:
            return [], []

        candidates_only = [text for _, text in uncertain]
        rescored = self._bulk_score_with(self._secondary, query, candidates_only)
        rescored_map: dict[str, float] = {text: s for s, text in rescored}

        threshold = self._config.secondary_accept_threshold or self._config.accept_threshold
        promoted: list[tuple[float, str]] = []
        demoted: list[tuple[float, str]] = []

        for primary_score, text in uncertain:
            secondary_score = rescored_map.get(text, 0.0)
            # Use the higher of primary and secondary scores for promoted items
            final_score = max(primary_score, secondary_score)
            if secondary_score >= threshold:
                promoted.append((final_score, text))
            else:
                demoted.append((primary_score, text))

        return promoted, demoted

    def ternary_recall(
        self, query: str, candidates: list[str]
    ) -> list[tuple[float, str]]:
        """Full ternary pipeline: score → classify → resolve → merge.

        Convenience method that runs the complete ternary retrieval pipeline
        and returns a single sorted list of accepted + promoted candidates.
        """
        result = self.ternary_score(query, candidates)
        promoted, _ = self.resolve_uncertain(query, result.uncertain)
        merged = result.accepted + promoted
        return sorted(merged, key=lambda x: x[0], reverse=True)

    # ── Internal helpers ──────────────────────────────────────────────

    @staticmethod
    def _bulk_score_with(
        strategy: MemoryRecallStrategy,
        query: str,
        candidates: list[str],
    ) -> list[tuple[float, str]]:
        """Call bulk_score if available, else fall back to per-item score."""
        bulk_fn = getattr(strategy, "bulk_score", None)
        if bulk_fn is not None and callable(bulk_fn):
            return bulk_fn(query, candidates)
        # Per-item fallback
        scored: list[tuple[float, str]] = []
        for c in candidates:
            s = strategy.score(query, c)
            if s > 0:
                scored.append((s, c))
        return sorted(scored, key=lambda x: x[0], reverse=True)


class TriternaryRecallStrategy(MemoryRecallStrategy):
    """Native ternary scorer: score() returns {-1.0, 0.0, 1.0} directly.

    Unlike TernaryRecallStrategy (which wraps a float scorer and classifies
    the float output post-hoc, with secondary scoring for the uncertain band),
    TriternaryRecallStrategy snaps the primary score to one of three discrete
    values using TernaryConfig thresholds.  The discrete value is what callers
    receive from score() — no further resolution step is performed.
    """

    def __init__(
        self,
        primary: MemoryRecallStrategy,
        config: TernaryConfig | None = None,
    ) -> None:
        self._primary = primary
        self._config = config or TernaryConfig()

    def score(self, query: str, candidate: str) -> float:
        """Return {-1.0, 0.0, 1.0} by snapping the primary float score."""
        raw = self._primary.score(query, candidate)
        if raw >= self._config.accept_threshold:
            return float(TernaryTier.ACCEPT)    # 1.0
        if raw < self._config.reject_threshold:
            return float(TernaryTier.REJECT)    # -1.0
        return float(TernaryTier.UNCERTAIN)     # 0.0

    def recall(
        self,
        query: str,
        candidates: list[str],
        max_results: int = 10,
    ) -> list[str]:
        """Return only ACCEPT-tier candidates, up to max_results."""
        accepted = [
            c for c in candidates
            if self.score(query, c) == float(TernaryTier.ACCEPT)
        ]
        return accepted[:max_results]

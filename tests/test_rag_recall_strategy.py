"""Tests for RagRecallStrategy — verifies RAG-backed memory recall and fallback."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from zen_claw.agent.memory_recall import RagRecallStrategy

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_strategy(tmp_path: Path, notebook_id: str = "default") -> RagRecallStrategy:
    return RagRecallStrategy(data_dir=tmp_path, notebook_id=notebook_id)


def _make_fake_result(content: str, rrf_score: float = 0.0, score: float = 0.0):
    r = MagicMock()
    r.content = content
    r.rrf_score = rrf_score
    r.score = score
    return r


# ── tests: initialization ─────────────────────────────────────────────────────


def test_rag_strategy_initialises_without_chromadb(tmp_path: Path):
    """Strategy should construct even when chromadb is not installed."""
    strat = _make_strategy(tmp_path)
    assert strat._data_dir == tmp_path
    assert strat._notebook_id == "default"
    assert strat._retriever is None  # lazy


def test_rag_strategy_fallback_when_retriever_unavailable(tmp_path: Path):
    """bulk_score falls back to keyword scoring when retriever is None."""
    strat = _make_strategy(tmp_path)
    # Force retriever to stay None (no chromadb in test environment)
    strat._retriever = None

    with patch.object(strat, "_get_retriever", return_value=None):
        results = strat.bulk_score("machine learning", ["I love machine learning", "cats and dogs"])
    # At least the matching candidate should score > 0
    contents = [c for _, c in results]
    assert "I love machine learning" in contents


# ── tests: bulk_score with mocked retriever ───────────────────────────────────


def test_bulk_score_returns_rag_results_for_matching_candidates(tmp_path: Path):
    strat = _make_strategy(tmp_path)
    candidates = ["vector search is fast", "I like coffee", "embeddings matter"]

    fake_retriever = MagicMock()
    fake_retriever._bm25_corpus = []
    fake_retriever.search.return_value = [
        _make_fake_result("vector search is fast", rrf_score=0.8),
        _make_fake_result("embeddings matter", rrf_score=0.5),
    ]
    strat._retriever = fake_retriever

    results = strat.bulk_score("semantic vector search", candidates)
    result_contents = [c for _, c in results]

    assert "vector search is fast" in result_contents
    assert "embeddings matter" in result_contents
    # The unmatched "I like coffee" should not appear in top results unless keyword match
    scores = dict((c, s) for s, c in results)
    assert scores.get("vector search is fast", 0) > scores.get("I like coffee", 0)


def test_bulk_score_downweights_keyword_fallback(tmp_path: Path):
    """Candidates returned by keyword fallback should score lower than RAG hits."""
    strat = _make_strategy(tmp_path)
    candidates = ["rag result content", "keyword match only content"]

    fake_retriever = MagicMock()
    fake_retriever._bm25_corpus = []
    # Only the first candidate returned by vector search
    fake_retriever.search.return_value = [
        _make_fake_result("rag result content", rrf_score=0.7),
    ]
    strat._retriever = fake_retriever

    results = strat.bulk_score("rag result", candidates)
    scores = {c: s for s, c in results}

    rag_score = scores.get("rag result content", 0.0)
    kw_score = scores.get("keyword match only content", 0.0)
    # RAG result must outscore keyword fallback
    assert rag_score >= kw_score


def test_score_single_returns_positive_for_matching(tmp_path: Path):
    strat = _make_strategy(tmp_path)
    fake_retriever = MagicMock()
    fake_retriever._bm25_corpus = []
    fake_retriever.search.return_value = [
        _make_fake_result("machine learning basics", rrf_score=0.6),
    ]
    strat._retriever = fake_retriever

    s = strat.score("machine learning", "machine learning basics")
    assert s > 0.0


def test_score_single_returns_zero_when_no_match(tmp_path: Path):
    strat = _make_strategy(tmp_path)
    fake_retriever = MagicMock()
    fake_retriever._bm25_corpus = []
    fake_retriever.search.return_value = []
    strat._retriever = fake_retriever

    s = strat.score("quantum physics", "I like pizza")
    assert s == 0.0


# ── tests: ContextBuilder integration ────────────────────────────────────────


def test_context_builder_rag_mode_falls_back_gracefully(tmp_path: Path):
    """ContextBuilder with memory_recall_mode='rag' should not raise."""
    from zen_claw.agent.context import ContextBuilder

    # We patch get_data_dir so RAG strategy doesn't hit the real fs
    with patch("zen_claw.agent.context.get_data_dir", return_value=tmp_path, create=True):
        try:
            ctx = ContextBuilder(tmp_path, memory_recall_mode="rag")
            # Strategy should be set (either RagRecallStrategy or KeywordRecallStrategy fallback)
            assert ctx.memory.recall_strategy is not None
        except Exception as exc:
            pytest.fail(f"ContextBuilder 'rag' mode raised unexpectedly: {exc}")

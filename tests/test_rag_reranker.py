"""Tests for LLMReranker."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from zen_claw.knowledge.reranker import LLMReranker, build_reranker


def _make_candidates(n: int):
    from zen_claw.knowledge.retriever import HybridSearchResult

    return [
        HybridSearchResult(
            content=f"passage {i}",
            source=f"doc{i}.txt",
            score=1.0 / i,
            rrf_score=1.0 / i,
        )
        for i in range(1, n + 1)
    ]


def _make_provider(content: str) -> MagicMock:
    response = MagicMock()
    response.content = content
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=response)
    return provider


class TestLLMReranker:
    @pytest.mark.asyncio
    async def test_rerank_reorders_by_llm_indices(self) -> None:
        """Reranker should reorder candidates according to LLM-returned indices."""
        candidates = _make_candidates(5)
        # LLM says: best order is 3, 1, 5, 2, 4
        provider = _make_provider(json.dumps([3, 1, 5, 2, 4]))
        reranker = LLMReranker(provider, fetch_top_n=20)

        result = await reranker.rerank("query", candidates, top_k=3)

        assert len(result) == 3
        assert result[0].content == "passage 3"
        assert result[1].content == "passage 1"
        assert result[2].content == "passage 5"

    @pytest.mark.asyncio
    async def test_rerank_returns_top_k(self) -> None:
        """Result length must equal top_k when enough candidates are available."""
        candidates = _make_candidates(10)
        provider = _make_provider(json.dumps(list(range(10, 0, -1))))
        reranker = LLMReranker(provider)

        result = await reranker.rerank("q", candidates, top_k=4)
        assert len(result) == 4

    @pytest.mark.asyncio
    async def test_fallback_on_invalid_json(self) -> None:
        """Unparseable LLM response should fall back to original order."""
        candidates = _make_candidates(5)
        provider = _make_provider("Sorry, I cannot rank these.")
        reranker = LLMReranker(provider)

        result = await reranker.rerank("q", candidates, top_k=3)

        assert len(result) == 3
        # Original order preserved
        assert result[0].content == "passage 1"

    @pytest.mark.asyncio
    async def test_fallback_on_provider_exception(self) -> None:
        """LLM call failure should fall back silently."""
        candidates = _make_candidates(5)
        provider = MagicMock()
        provider.chat = AsyncMock(side_effect=RuntimeError("network error"))
        reranker = LLMReranker(provider)

        result = await reranker.rerank("q", candidates, top_k=2)

        assert len(result) == 2
        assert result[0].content == "passage 1"

    @pytest.mark.asyncio
    async def test_skip_rerank_when_candidates_lt_top_k(self) -> None:
        """When candidates count < top_k (strictly less), no LLM call should be made."""
        candidates = _make_candidates(3)
        provider = _make_provider("[1, 2, 3]")
        reranker = LLMReranker(provider)

        result = await reranker.rerank("q", candidates, top_k=5)

        provider.chat.assert_not_called()
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_empty_candidates_returns_empty(self) -> None:
        provider = _make_provider("[1]")
        reranker = LLMReranker(provider)
        result = await reranker.rerank("q", [], top_k=5)
        assert result == []

    @pytest.mark.asyncio
    async def test_partial_index_list_appends_remainder(self) -> None:
        """Indices not mentioned by LLM should be appended in original order."""
        candidates = _make_candidates(5)
        # LLM only mentions 3 out of 5
        provider = _make_provider(json.dumps([5, 3, 1]))
        reranker = LLMReranker(provider)

        result = await reranker.rerank("q", candidates, top_k=5)

        assert len(result) == 5
        assert result[0].content == "passage 5"
        assert result[1].content == "passage 3"
        assert result[2].content == "passage 1"
        # 2 and 4 appended
        remaining = {result[3].content, result[4].content}
        assert remaining == {"passage 2", "passage 4"}

    @pytest.mark.asyncio
    async def test_json_array_embedded_in_prose(self) -> None:
        """Reranker should extract JSON array from verbose LLM response."""
        candidates = _make_candidates(4)
        prose = "Here is my ranking based on relevance: [2, 4, 1, 3]. Hope this helps!"
        provider = _make_provider(prose)
        reranker = LLMReranker(provider)

        result = await reranker.rerank("q", candidates, top_k=4)

        assert result[0].content == "passage 2"
        assert result[1].content == "passage 4"


class TestBuildReranker:
    def test_returns_none_when_provider_is_none(self) -> None:
        assert build_reranker(None) is None

    def test_returns_reranker_with_provider(self) -> None:
        provider = MagicMock()
        reranker = build_reranker(provider, fetch_top_n=15)
        assert isinstance(reranker, LLMReranker)
        assert reranker.fetch_top_n == 15


class TestPipelineRerank:
    @pytest.mark.asyncio
    async def test_search_rerank_false_no_reranker_called(self, tmp_path) -> None:
        """rerank=False should never call the reranker."""
        from zen_claw.knowledge.pipeline import RAGPipeline

        provider = _make_provider("[1]")
        reranker = LLMReranker(provider)
        pipeline = RAGPipeline(tmp_path, reranker=reranker)

        # notebook not found → ValueError, but the reranker must NOT have been called
        with pytest.raises(ValueError, match="notebook not found"):
            pipeline.search("query", rerank=False)

        provider.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_search_no_reranker_rerank_true_graceful(self, tmp_path) -> None:
        """rerank=True without a configured reranker should not raise."""
        from zen_claw.knowledge.pipeline import RAGPipeline

        pipeline = RAGPipeline(tmp_path, reranker=None)

        with pytest.raises(ValueError, match="notebook not found"):
            pipeline.search("query", rerank=True)
        # No exception from reranker path itself

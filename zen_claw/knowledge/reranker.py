"""LLM-based reranker for post-retrieval candidate re-scoring."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from zen_claw.knowledge.retriever import HybridSearchResult
    from zen_claw.providers.base import LLMProvider

_MAX_CONTENT_CHARS = 400  # chars shown per candidate in reranking prompt


class LLMReranker:
    """Rerank retrieval candidates using an LLM relevance judgement.

    The reranker asks the LLM to return a JSON array of 1-based indices ordered
    by relevance (most relevant first).  When the LLM call fails or returns an
    unparseable response the reranker falls back to the original RRF order so
    downstream callers always receive a valid result.
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        fetch_top_n: int = 20,
    ) -> None:
        self._provider = provider
        self._fetch_top_n = max(1, int(fetch_top_n))

    @property
    def fetch_top_n(self) -> int:
        """Number of candidates to pass to the LLM for reranking."""
        return self._fetch_top_n

    async def rerank(
        self,
        query: str,
        candidates: list[HybridSearchResult],
        top_k: int = 5,
    ) -> list[HybridSearchResult]:
        """Return at most *top_k* candidates re-ordered by LLM relevance.

        Falls back to the original order if the LLM call fails or the response
        cannot be parsed.  Never raises.
        """
        if not candidates:
            return []
        top_k = max(1, int(top_k))
        if len(candidates) < top_k:
            # Fewer candidates than requested — nothing meaningful to rerank.
            return list(candidates)

        try:
            prompt = self._build_prompt(query, candidates)
            response = await self._provider.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You rank text passages by their relevance to a query. "
                            "Return ONLY a JSON array of 1-based passage indices "
                            "ordered from most relevant to least relevant. "
                            "Example: [3, 1, 4, 2]"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
            )
            indices = self._parse_ranking(response.content, len(candidates))
            if indices:
                return self._apply_ranking(candidates, indices, top_k)
        except Exception:  # noqa: BLE001
            pass

        # Graceful fallback: original RRF order.
        return list(candidates[:top_k])

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_prompt(query: str, candidates: list[HybridSearchResult]) -> str:
        lines = [f"Query: {query}\n", "Passages:"]
        for i, c in enumerate(candidates, 1):
            snippet = str(c.content or "")[:_MAX_CONTENT_CHARS].replace("\n", " ")
            lines.append(f"{i}. {snippet}")
        lines.append("\nReturn a JSON array of indices ranked by relevance to the query.")
        return "\n".join(lines)

    @staticmethod
    def _parse_ranking(raw: str | None, n: int) -> list[int]:
        """Extract a list of valid 1-based indices from the raw LLM output."""
        body = (raw or "").strip()
        if not body:
            return []

        def _extract(text: str) -> list[int]:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                return []
            if not isinstance(data, list):
                return []
            result: list[int] = []
            seen: set[int] = set()
            for item in data:
                try:
                    idx = int(item)
                except (TypeError, ValueError):
                    continue
                if 1 <= idx <= n and idx not in seen:
                    seen.add(idx)
                    result.append(idx)
            return result

        # Try direct parse first.
        indices = _extract(body)
        if indices:
            return indices

        # Try to extract the first JSON array from a longer response.
        match = re.search(r"\[[\d,\s]+\]", body)
        if match:
            return _extract(match.group(0))

        return []

    @staticmethod
    def _apply_ranking(
        candidates: list[HybridSearchResult],
        indices: list[int],
        top_k: int,
    ) -> list[HybridSearchResult]:
        """Build re-ordered result list from validated index sequence."""
        seen: set[int] = set()
        reranked: list[HybridSearchResult] = []

        for idx in indices:
            if idx not in seen:
                seen.add(idx)
                reranked.append(candidates[idx - 1])

        # Append any candidates not mentioned by the LLM.
        for i, c in enumerate(candidates, 1):
            if i not in seen:
                reranked.append(c)

        return reranked[:top_k]


def build_reranker(provider: Any, *, fetch_top_n: int = 20) -> LLMReranker | None:
    """Convenience factory — returns None when *provider* is None."""
    if provider is None:
        return None
    return LLMReranker(provider, fetch_top_n=fetch_top_n)

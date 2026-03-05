"""Memory recall strategy interfaces and default implementation."""

import re
from abc import ABC, abstractmethod


class MemoryRecallStrategy(ABC):
    """Scores candidate memory snippets against a user query."""

    @abstractmethod
    def score(self, query: str, candidate: str) -> float:
        pass


class NoopRecallStrategy(MemoryRecallStrategy):
    """Disable recall ranking; always returns zero score."""

    def score(self, query: str, candidate: str) -> float:
        return 0.0


class KeywordRecallStrategy(MemoryRecallStrategy):
    """Simple lexical overlap scorer for lightweight memory recall."""

    _stopwords = {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "i",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "was",
        "what",
        "when",
        "where",
        "which",
        "with",
        "you",
    }

    def score(self, query: str, candidate: str) -> float:
        q_tokens = self._tokens(query)
        c_tokens = self._tokens(candidate)
        if not q_tokens or not c_tokens:
            return 0.0
        overlap = len(q_tokens.intersection(c_tokens))
        if overlap == 0:
            return 0.0
        # Lightweight normalized score.
        return overlap / max(1, len(q_tokens))

    def _tokens(self, s: str) -> set[str]:
        tokens = {
            t
            for t in re.findall(r"[A-Za-z0-9_]+", s.lower())
            if len(t) > 1 and t not in self._stopwords
        }
        return tokens


class RagRecallStrategy(MemoryRecallStrategy):
    """Memory recall backed by the hybrid RAG knowledge base (vector + BM25).

    Scores candidates by querying the RAG index and mapping rrf_score back to
    each candidate string.  Falls back to KeywordRecallStrategy if the knowledge
    stack (chromadb / sentence-transformers) is not available.
    """

    def __init__(self, data_dir: "Path", notebook_id: str = "default"):  # noqa: F821
        from pathlib import Path as _Path

        self._data_dir = _Path(data_dir)
        self._notebook_id = notebook_id or "default"
        self._fallback = KeywordRecallStrategy()
        self._retriever = None  # lazily loaded

    def _get_retriever(self):
        if self._retriever is not None:
            return self._retriever
        try:
            from zen_claw.knowledge.notebook import NotebookManager
            from zen_claw.knowledge.retriever import HybridRetriever

            manager = NotebookManager(self._data_dir)
            nb = manager.get_or_create(self._notebook_id)
            self._retriever = HybridRetriever.from_notebook(nb, self._data_dir)
        except Exception:
            self._retriever = None
        return self._retriever

    def _sync_corpus(self, candidates: list[str]) -> None:
        """Add unseen candidates to the BM25 corpus so they are retrievable."""
        retriever = self._get_retriever()
        if retriever is None:
            return
        existing = {row.get("content", "") for row in retriever._bm25_corpus}
        new_rows = [
            {"content": c, "source": "memory", "page": None}
            for c in candidates
            if c not in existing
        ]
        if new_rows:
            retriever._bm25_corpus.extend(new_rows)

    def score(self, query: str, candidate: str) -> float:
        results = self.bulk_score(query, [candidate])
        if results:
            return results[0][0]
        return 0.0

    def bulk_score(self, query: str, candidates: list[str]) -> list[tuple[float, str]]:
        retriever = self._get_retriever()
        if retriever is None:
            # Graceful degradation
            return [
                (self._fallback.score(query, c), c)
                for c in candidates
                if self._fallback.score(query, c) > 0
            ]

        self._sync_corpus(candidates)
        candidate_set = set(candidates)
        try:
            results = retriever.search(query=query, top_k=min(50, max(5, len(candidates))))
        except Exception:
            return [
                (self._fallback.score(query, c), c)
                for c in candidates
                if self._fallback.score(query, c) > 0
            ]

        scored: list[tuple[float, str]] = []
        matched = set()
        for r in results:
            if r.content in candidate_set:
                scored.append((float(r.rrf_score or r.score), r.content))
                matched.add(r.content)

        # For candidates not found by retriever, apply keyword fallback
        for c in candidates:
            if c not in matched:
                kw = self._fallback.score(query, c)
                if kw > 0:
                    scored.append((kw * 0.3, c))  # Downweight vs. RAG results

        return sorted(scored, key=lambda x: x[0], reverse=True)

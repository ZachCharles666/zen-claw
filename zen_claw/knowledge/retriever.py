"""Hybrid retriever with vector + lexical fusion."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zen_claw.knowledge.chunker import TextChunker
from zen_claw.knowledge.ingestor import Document
from zen_claw.knowledge.notebook import Notebook
from zen_claw.knowledge.store import ChromaStore, SearchResult


@dataclass
class HybridSearchResult:
    content: str
    source: str
    score: float
    page: int | None = None
    rrf_score: float = 0.0


class HybridRetriever:
    def __init__(self, vector_store: ChromaStore):
        self._vector = vector_store
        self._chunker = TextChunker()
        self._bm25_corpus: list[dict[str, Any]] = []

    @classmethod
    def from_notebook(cls, notebook: Notebook, data_dir: Path) -> "HybridRetriever":
        store = ChromaStore(collection_id=notebook.id, data_dir=data_dir)
        return cls(vector_store=store)

    async def add_documents(self, docs: list[Document]) -> int:
        chunks: list[dict[str, Any]] = []
        for doc in docs:
            for chunk in self._chunker.chunk_with_metadata(
                doc.content, source=doc.source, page=doc.page
            ):
                chunks.append(chunk)
        if not chunks:
            return 0
        self._vector.add(chunks)
        self._bm25_corpus.extend(chunks)
        return len(chunks)

    def search(self, query: str, top_k: int = 5) -> list[HybridSearchResult]:
        vec = self._vector.search(query=query, top_k=top_k)
        vec_rank = {r.content: i + 1 for i, r in enumerate(vec)}
        lex = self._lexical_search(query=query, top_k=top_k)
        lex_rank = {r.content: i + 1 for i, r in enumerate(lex)}

        merged: dict[str, HybridSearchResult] = {}
        c = 60.0
        for item in vec:
            merged[item.content] = HybridSearchResult(
                content=item.content,
                source=item.source,
                page=item.page,
                score=item.score,
                rrf_score=1.0 / (c + vec_rank[item.content]),
            )
        for item in lex:
            existing = merged.get(item.content)
            add = 1.0 / (c + lex_rank[item.content])
            if existing is None:
                merged[item.content] = HybridSearchResult(
                    content=item.content,
                    source=item.source,
                    page=item.page,
                    score=item.score,
                    rrf_score=add,
                )
            else:
                existing.rrf_score += add
        rows = sorted(merged.values(), key=lambda r: r.rrf_score, reverse=True)
        return rows[: max(1, int(top_k))]

    def _lexical_search(self, query: str, top_k: int) -> list[SearchResult]:
        if not self._bm25_corpus:
            return []
        try:
            from rank_bm25 import BM25Okapi
        except ImportError:
            q = query.lower()
            hits = []
            for row in self._bm25_corpus:
                content = str(row.get("content", ""))
                if q in content.lower():
                    hits.append(
                        SearchResult(
                            content=content,
                            source=str(row.get("source", "")),
                            page=row.get("page"),
                            score=1.0,
                        )
                    )
            return hits[:top_k]
        tokenized = [str(row.get("content", "")).lower().split() for row in self._bm25_corpus]
        bm25 = BM25Okapi(tokenized)
        scores = bm25.get_scores(query.lower().split())
        pairs = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)[:top_k]
        out: list[SearchResult] = []
        for idx, score in pairs:
            row = self._bm25_corpus[idx]
            out.append(
                SearchResult(
                    content=str(row.get("content", "")),
                    source=str(row.get("source", "")),
                    page=row.get("page"),
                    score=float(score),
                )
            )
        return out

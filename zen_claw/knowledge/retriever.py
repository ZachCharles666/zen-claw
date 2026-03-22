"""Hybrid retriever with vector + lexical fusion."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zen_claw.knowledge.chunker import TextChunker
from zen_claw.knowledge.ingestor import Document
from zen_claw.knowledge.notebook import Notebook
from zen_claw.knowledge.store import SearchResult, VectorStore, create_vector_store


@dataclass
class HybridSearchResult:
    content: str
    source: str
    score: float
    page: int | None = None
    rrf_score: float = 0.0


class HybridRetriever:
    def __init__(self, vector_store: VectorStore):
        self._vector = vector_store
        self._chunker = TextChunker()
        self._bm25_corpus: list[dict[str, Any]] = []

    @classmethod
    def from_notebook(
        cls,
        notebook: Notebook,
        data_dir: Path,
        *,
        store_kind: str = "chroma",
        store_options: dict[str, Any] | None = None,
    ) -> "HybridRetriever":
        store = create_vector_store(
            collection_id=notebook.id,
            data_dir=data_dir,
            store_kind=store_kind,
            store_options=store_options,
        )
        return cls(vector_store=store)

    async def add_documents(self, docs: list[Document]) -> int:
        chunks: list[dict[str, Any]] = []
        for doc in docs:
            for chunk in self._chunker.chunk_with_metadata(
                doc.content, source=doc.source, page=doc.page, metadata=doc.metadata
            ):
                chunks.append(chunk)
        if not chunks:
            return 0
        self._vector.add(chunks)
        self._bm25_corpus.extend(chunks)
        return len(chunks)

    def search(
        self, query: str, top_k: int = 5, filters: dict[str, Any] | None = None
    ) -> list[HybridSearchResult]:
        vec = self._vector.search(query=query, top_k=top_k, filters=filters)
        vec_rank = {r.content: i + 1 for i, r in enumerate(vec)}
        lex = self._lexical_search(query=query, top_k=top_k, filters=filters)
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

    def backend_diagnostics(self) -> dict[str, Any]:
        if hasattr(self._vector, "diagnostics"):
            return dict(self._vector.diagnostics())
        return {}

    def _lexical_search(
        self, query: str, top_k: int, filters: dict[str, Any] | None = None
    ) -> list[SearchResult]:
        if not self._bm25_corpus:
            return []
        rows = self._apply_filters(self._bm25_corpus, filters=filters)
        if not rows:
            return []
        try:
            from rank_bm25 import BM25Okapi
        except ImportError:
            q = query.lower()
            hits = []
            for row in rows:
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
        tokenized = [str(row.get("content", "")).lower().split() for row in rows]
        bm25 = BM25Okapi(tokenized)
        scores = bm25.get_scores(query.lower().split())
        pairs = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)[:top_k]
        out: list[SearchResult] = []
        for idx, score in pairs:
            row = rows[idx]
            out.append(
                SearchResult(
                    content=str(row.get("content", "")),
                    source=str(row.get("source", "")),
                    page=row.get("page"),
                    score=float(score),
                    metadata=dict(row.get("metadata") or {}),
                )
            )
        return out

    @staticmethod
    def _apply_filters(
        rows: list[dict[str, Any]], filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        if not filters:
            return list(rows)
        filtered: list[dict[str, Any]] = []
        for row in rows:
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            ok = True
            for key, expected in filters.items():
                if str(metadata.get(key)) != str(expected):
                    ok = False
                    break
            if ok:
                filtered.append(row)
        return filtered

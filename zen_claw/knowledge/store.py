"""Vector store abstraction with Chroma implementation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zen_claw.knowledge.embedder import Embedder, LocalEmbedder


@dataclass
class SearchResult:
    content: str
    source: str
    score: float
    page: int | None = None
    metadata: dict[str, Any] | None = None


class ChromaStore:
    def __init__(self, collection_id: str, data_dir: Path, embedder: Embedder | None = None):
        try:
            import chromadb
        except ImportError as exc:
            raise RuntimeError("chromadb is required for ChromaStore") from exc
        self._embedder = embedder or LocalEmbedder()
        self._client = chromadb.PersistentClient(
            path=str((Path(data_dir) / "knowledge" / "chroma").resolve())
        )
        self._collection = self._client.get_or_create_collection(name=collection_id)

    def add(self, chunks: list[dict[str, Any]]) -> list[str]:
        if not chunks:
            return []
        ids = [str(uuid.uuid4()) for _ in chunks]
        texts = [str(c.get("content", "")) for c in chunks]
        metas = [_build_meta(c) for c in chunks]
        vectors = self._embedder.embed(texts)
        self._collection.add(ids=ids, documents=texts, metadatas=metas, embeddings=vectors)
        return ids

    def count(self) -> int:
        return int(self._collection.count())

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        if self.count() == 0:
            return []
        query_vec = self._embedder.embed([query])[0]
        n = min(max(1, int(top_k)), max(1, self.count()))
        data = self._collection.query(query_embeddings=[query_vec], n_results=n)
        docs = (data.get("documents") or [[]])[0]
        metas = (data.get("metadatas") or [[]])[0]
        dists = (data.get("distances") or [[]])[0]
        out: list[SearchResult] = []
        for doc, meta, dist in zip(docs, metas, dists):
            score = 1.0 / (1.0 + float(dist if dist is not None else 1.0))
            out.append(
                SearchResult(
                    content=str(doc or ""),
                    source=str((meta or {}).get("source", "")),
                    page=(meta or {}).get("page"),
                    score=score,
                    metadata=meta or {},
                )
            )
        return out

    def delete_by_source(self, source: str) -> int:
        data = self._collection.get(where={"source": str(source)})
        ids = list(data.get("ids") or [])
        if not ids:
            return 0
        self._collection.delete(ids=ids)
        return len(ids)


def _build_meta(chunk: dict[str, Any]) -> dict[str, Any]:
    meta: dict[str, Any] = {"source": str(chunk.get("source", ""))}
    page = chunk.get("page")
    if page is not None:
        meta["page"] = page
    idx = chunk.get("chunk_index")
    if idx is not None:
        meta["chunk_index"] = idx
    return meta

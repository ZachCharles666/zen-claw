"""Vector store abstraction with Chroma implementation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from zen_claw.knowledge.embedder import Embedder, LocalEmbedder

_IN_MEMORY_REGISTRY: dict[tuple[str, str], list[dict[str, Any]]] = {}


@dataclass
class SearchResult:
    content: str
    source: str
    score: float
    page: int | None = None
    metadata: dict[str, Any] | None = None


class VectorStore(Protocol):
    def add(self, chunks: list[dict[str, Any]]) -> list[str]: ...

    def count(self) -> int: ...

    def diagnostics(self) -> dict[str, Any]: ...

    def search(
        self, query: str, top_k: int = 5, filters: dict[str, Any] | None = None
    ) -> list[SearchResult]: ...

    def delete_by_source(self, source: str) -> int: ...


class ChromaStore:
    def __init__(
        self,
        collection_id: str,
        data_dir: Path,
        embedder: Embedder | None = None,
        *,
        chroma_subdir: str = "chroma",
        collection_name: str = "",
    ):
        try:
            import chromadb
        except ImportError as exc:
            raise RuntimeError("chromadb is required for ChromaStore") from exc
        self._embedder = embedder or LocalEmbedder()
        self._storage_path = (
            Path(data_dir) / "knowledge" / (str(chroma_subdir).strip() or "chroma")
        ).resolve()
        self._collection_name = str(collection_name or collection_id)
        self._client = chromadb.PersistentClient(
            path=str(self._storage_path)
        )
        self._collection = self._client.get_or_create_collection(name=self._collection_name)

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

    def diagnostics(self) -> dict[str, Any]:
        storage_exists = self._storage_path.exists()
        chunk_count = self.count()
        return {
            "backend": "chroma",
            "healthy": True,
            "collection_name": self._collection_name,
            "storage_path": str(self._storage_path),
            "storage_present": storage_exists,
            "storage_bytes": _dir_size_bytes(self._storage_path) if storage_exists else 0,
            "chunk_count": chunk_count,
        }

    def search(
        self, query: str, top_k: int = 5, filters: dict[str, Any] | None = None
    ) -> list[SearchResult]:
        if self.count() == 0:
            return []
        query_vec = self._embedder.embed([query])[0]
        n = min(max(1, int(top_k)), max(1, self.count()))
        where = _build_where(filters)
        kwargs: dict[str, Any] = {"query_embeddings": [query_vec], "n_results": n}
        if where:
            kwargs["where"] = where
        data = self._collection.query(**kwargs)
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


class InMemoryStore:
    """Simple in-process store used for tests and non-persistent fallback paths."""

    def __init__(
        self,
        collection_id: str,
        data_dir: Path,
        embedder: Embedder | None = None,
        *,
        namespace: str = "",
    ):
        _ = embedder
        self._namespace = str(namespace or "").strip()
        self._collection_id = str(collection_id)
        self._data_dir = str(Path(data_dir).resolve())
        registry_key = (
            self._data_dir,
            self._namespace,
            self._collection_id,
        )
        self._rows = _IN_MEMORY_REGISTRY.setdefault(registry_key, [])

    def add(self, chunks: list[dict[str, Any]]) -> list[str]:
        ids: list[str] = []
        for chunk in chunks:
            row = dict(chunk)
            row_id = str(uuid.uuid4())
            row["_id"] = row_id
            self._rows.append(row)
            ids.append(row_id)
        return ids

    def count(self) -> int:
        return len(self._rows)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "backend": "memory",
            "healthy": True,
            "namespace": self._namespace,
            "collection_name": self._collection_id,
            "storage_path": "",
            "storage_present": False,
            "storage_bytes": 0,
            "chunk_count": self.count(),
        }

    def search(
        self, query: str, top_k: int = 5, filters: dict[str, Any] | None = None
    ) -> list[SearchResult]:
        query_lower = query.lower()
        rows = []
        for row in self._rows:
            metadata = dict(row.get("metadata") or {})
            if filters and any(str(metadata.get(k)) != str(v) for k, v in filters.items()):
                continue
            content = str(row.get("content", ""))
            score = 1.0 if query_lower in content.lower() else 0.1
            rows.append(
                SearchResult(
                    content=content,
                    source=str(row.get("source", "")),
                    page=row.get("page"),
                    score=score,
                    metadata=metadata,
                )
            )
        rows.sort(key=lambda item: item.score, reverse=True)
        return rows[: max(1, int(top_k))]

    def delete_by_source(self, source: str) -> int:
        before = len(self._rows)
        self._rows[:] = [
            row for row in self._rows if str(row.get("source", "")) != str(source)
        ]
        return before - len(self._rows)


def create_vector_store(
    collection_id: str,
    data_dir: Path,
    *,
    store_kind: str = "chroma",
    embedder: Embedder | None = None,
    store_options: dict[str, Any] | None = None,
) -> VectorStore:
    kind = str(store_kind or "chroma").strip().lower()
    options = dict(store_options or {})
    chroma_subdir = str(options.get("chroma_subdir") or "chroma").strip() or "chroma"
    collection_prefix = str(options.get("chroma_collection_prefix") or "").strip()
    collection_name = f"{collection_prefix}{collection_id}" if collection_prefix else str(collection_id)
    memory_namespace = str(options.get("memory_namespace") or "").strip()
    if kind == "memory":
        return InMemoryStore(
            collection_id=collection_id,
            data_dir=data_dir,
            embedder=embedder,
            namespace=memory_namespace,
        )
    if kind == "chroma":
        return ChromaStore(
            collection_id=collection_id,
            data_dir=data_dir,
            embedder=embedder,
            chroma_subdir=chroma_subdir,
            collection_name=collection_name,
        )
    raise ValueError(f"unsupported vector store: {store_kind}")


def _build_meta(chunk: dict[str, Any]) -> dict[str, Any]:
    meta: dict[str, Any] = {"source": str(chunk.get("source", ""))}
    page = chunk.get("page")
    if page is not None:
        meta["page"] = page
    idx = chunk.get("chunk_index")
    if idx is not None:
        meta["chunk_index"] = idx
    raw_metadata = chunk.get("metadata")
    if isinstance(raw_metadata, dict):
        for key, value in raw_metadata.items():
            normalized_key = str(key).strip()
            if not normalized_key or normalized_key in meta:
                continue
            if isinstance(value, (str, int, float, bool)):
                meta[normalized_key] = value
    return meta


def _build_where(filters: dict[str, Any] | None) -> dict[str, Any] | None:
    if not filters:
        return None
    where: dict[str, Any] = {}
    for key, value in filters.items():
        normalized_key = str(key).strip()
        if not normalized_key:
            continue
        if isinstance(value, (str, int, float, bool)):
            where[normalized_key] = value
    return where or None


def _dir_size_bytes(path: Path) -> int:
    total = 0
    try:
        for child in path.rglob("*"):
            if child.is_file():
                total += child.stat().st_size
    except OSError:
        return 0
    return total

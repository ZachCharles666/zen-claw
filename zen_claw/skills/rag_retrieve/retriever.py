"""Thin product-facing retrieval wrapper built on the shared RAG pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zen_claw.auth.paths import DEFAULT_TENANT_ID
from zen_claw.knowledge.pipeline import RAGPipeline


@dataclass(frozen=True)
class RetrievedPassage:
    content: str
    source: str
    score: float
    page: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "source": self.source,
            "score": self.score,
            "page": self.page,
        }


class RAGRetrieverSkill:
    """Business-facing retrieval bridge for local notebook knowledge."""

    def __init__(
        self,
        data_dir: Path,
        default_notebook: str = "default",
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        store_kind: str = "",
    ):
        self._data_dir = Path(data_dir)
        self._default_notebook = default_notebook
        self._tenant_id = str(tenant_id or DEFAULT_TENANT_ID).strip() or DEFAULT_TENANT_ID
        self._store_kind = str(store_kind or "").strip()

    def retrieve(
        self,
        query: str,
        *,
        notebook_id: str = "",
        top_k: int = 5,
        source_contains: str = "",
        filters: dict[str, Any] | None = None,
        tenant_id: str = "",
        store_backend: str = "",
    ) -> dict[str, Any]:
        query = query.strip()
        if not query:
            raise ValueError("query is required")
        normalized_top_k = max(1, min(20, int(top_k)))
        normalized_filters = self._normalize_filters(filters)
        effective_tenant_id = str(tenant_id or self._tenant_id).strip() or DEFAULT_TENANT_ID
        effective_store_backend = str(store_backend or self._store_kind).strip()
        pipeline = RAGPipeline(
            self._data_dir,
            default_notebook=self._default_notebook,
            store_kind=effective_store_backend,
            tenant_id=effective_tenant_id,
        )
        payload = pipeline.search(
            query,
            notebook_id=notebook_id,
            top_k=normalized_top_k,
            source_contains=source_contains,
            filters=normalized_filters,
        )
        passages = [
            RetrievedPassage(
                content=str(row.get("content") or ""),
                source=str(row.get("source") or ""),
                score=float(row.get("score") or 0.0),
                page=row.get("page"),
            )
            for row in list(payload.get("results") or [])
        ]
        return {
            "notebook": str(payload.get("notebook") or notebook_id.strip() or self._default_notebook),
            "notebook_id": str(payload.get("notebook_id") or ""),
            "tenant_id": str(payload.get("tenant_id") or effective_tenant_id),
            "query": query,
            "top_k": normalized_top_k,
            "filters": normalized_filters,
            "source_contains": str(source_contains or "").strip(),
            "store_backend": str(payload.get("store_backend") or effective_store_backend),
            "results": [passage.to_dict() for passage in passages],
        }

    @staticmethod
    def _normalize_filters(filters: dict[str, Any] | None) -> dict[str, Any]:
        if not filters:
            return {}
        normalized: dict[str, Any] = {}
        for key, value in dict(filters or {}).items():
            name = str(key or "").strip()
            if not name:
                continue
            if isinstance(value, (str, int, float, bool)):
                normalized[name] = value
        return normalized

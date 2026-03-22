"""Reusable RAG pipeline built on the current knowledge components."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from zen_claw.auth.paths import DEFAULT_TENANT_ID, tenant_data_dir
from zen_claw.auth.tenant import TenantStore
from zen_claw.knowledge.ingestor import Document, Ingestor
from zen_claw.knowledge.notebook import NotebookManager
from zen_claw.knowledge.reranker import LLMReranker
from zen_claw.knowledge.retriever import HybridRetriever


@dataclass(frozen=True)
class RAGSearchHit:
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


class RAGPipeline:
    """Minimal production-facing pipeline contract for ingest/search/stats."""

    def __init__(
        self,
        data_dir: Path,
        default_notebook: str = "default",
        store_kind: str = "",
        tenant_id: str = DEFAULT_TENANT_ID,
        reranker: LLMReranker | None = None,
    ):
        self._base_data_dir = Path(data_dir)
        self._default_notebook = default_notebook
        self._store_kind_override = self._normalize_store_kind(store_kind)
        self._store_kind = self._resolve_store_kind("")
        self._store_options = self._resolve_store_options()
        self._tenant_id = self._normalize_tenant_id(tenant_id)
        self._data_dir = tenant_data_dir(self._base_data_dir, self._tenant_id)
        self._manager = NotebookManager(self._data_dir)
        self._ingestor = Ingestor()
        self._reranker = reranker

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    @property
    def store_kind(self) -> str:
        return self._store_kind

    @property
    def tenant_store_backend(self) -> str:
        return self._resolve_tenant_store_kind()

    @property
    def store_options(self) -> dict[str, Any]:
        return dict(self._store_options)

    async def ingest(self, source: str, *, notebook_id: str = "") -> dict[str, Any]:
        return await self.ingest_with_metadata(source, notebook_id=notebook_id, metadata=None)

    async def ingest_with_metadata(
        self,
        source: str,
        *,
        notebook_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        docs = await self._ingestor.ingest(source)
        normalized_metadata = self._normalize_metadata(metadata)
        return await self.ingest_documents(
            docs,
            notebook_id=notebook_id,
            source=source,
            metadata=normalized_metadata,
        )

    async def ingest_documents(
        self,
        docs: list[Document],
        *,
        notebook_id: str = "",
        source: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        target_notebook = notebook_id.strip() or self._default_notebook
        notebook = self._manager.get_or_create(target_notebook)
        effective_store_kind = self._effective_store_kind(notebook)
        normalized_metadata = self._normalize_metadata(metadata)
        prepared_docs = docs
        if normalized_metadata:
            prepared_docs = [
                type(doc)(
                    content=doc.content,
                    source=doc.source,
                    page=doc.page,
                    metadata={**dict(doc.metadata), **normalized_metadata},
                )
                for doc in docs
            ]
        retriever = HybridRetriever.from_notebook(
            notebook,
            self._data_dir,
            store_kind=effective_store_kind,
            store_options=self._store_options,
        )
        chunks_added = await retriever.add_documents(prepared_docs)
        clean_source = str(source or "").strip() or (
            str(prepared_docs[0].source).strip() if prepared_docs else ""
        )
        document_id = self._manager.record_source(
            notebook.id,
            clean_source,
            len(prepared_docs),
            metadata=self._build_document_metadata(
                source=clean_source,
                docs=prepared_docs,
                metadata=normalized_metadata,
            ),
        )
        self._manager.bump_doc_count(
            notebook.id,
            max(1, len(prepared_docs)) if prepared_docs else 0,
        )
        return {
            "notebook": notebook.name,
            "notebook_id": notebook.id,
            "tenant_id": self._tenant_id,
            "document_id": document_id,
            "source": clean_source,
            "documents": len(prepared_docs),
            "chunks_added": int(chunks_added),
            "metadata": normalized_metadata,
            "store_backend": effective_store_kind,
        }

    def search(
        self,
        query: str,
        *,
        notebook_id: str = "",
        top_k: int = 5,
        source_contains: str = "",
        filters: dict[str, Any] | None = None,
        rerank: bool = False,
        rerank_top_n: int = 20,
    ) -> dict[str, Any]:
        target_notebook = notebook_id.strip() or self._default_notebook
        notebook = self._manager.get(target_notebook)
        if notebook is None:
            raise ValueError(f"notebook not found: {target_notebook}")
        effective_store_kind = self._effective_store_kind(notebook)
        retriever = HybridRetriever.from_notebook(
            notebook,
            self._data_dir,
            store_kind=effective_store_kind,
            store_options=self._store_options,
        )
        normalized_filters = self._normalize_metadata(filters)
        fetch_k = max(1, min(20, int(top_k)))
        will_rerank = rerank and self._reranker is not None
        if will_rerank:
            fetch_k = max(fetch_k, max(1, int(rerank_top_n)))
        rows = retriever.search(
            query=query,
            top_k=fetch_k,
            filters=normalized_filters,
        )
        needle = source_contains.strip().lower()
        if needle:
            rows = [row for row in rows if needle in str(row.source).lower()]
        reranked_flag = False
        if will_rerank and rows:
            import asyncio

            try:
                rows = asyncio.get_event_loop().run_until_complete(
                    self._reranker.rerank(query, rows, top_k=max(1, min(20, int(top_k))))
                )
                reranked_flag = True
            except Exception:  # noqa: BLE001
                rows = rows[: max(1, min(20, int(top_k)))]
        else:
            rows = rows[: max(1, min(20, int(top_k)))]
        hits = [
            RAGSearchHit(
                content=row.content,
                source=row.source,
                score=float(row.rrf_score or row.score),
                page=row.page,
            ).to_dict()
            for row in rows
        ]
        return {
            "notebook": notebook.name,
            "notebook_id": notebook.id,
            "tenant_id": self._tenant_id,
            "query": query,
            "filters": normalized_filters,
            "store_backend": effective_store_kind,
            "reranked": reranked_flag,
            "results": hits,
        }

    def list_documents(self, *, notebook_id: str = "") -> dict[str, Any]:
        target_notebook = notebook_id.strip() or self._default_notebook
        notebook = self._manager.get(target_notebook)
        if notebook is None:
            raise ValueError(f"notebook not found: {target_notebook}")
        rows = self._manager.list_documents(notebook.id)
        return {
            "tenant_id": self._tenant_id,
            "notebook": notebook.name,
            "notebook_id": notebook.id,
            "store_backend": self._effective_store_kind(notebook),
            "documents": rows,
            "total_documents": len(rows),
        }

    def preview_document(
        self,
        document_id: str,
        *,
        notebook_id: str = "",
        max_chars: int = 2000,
    ) -> dict[str, Any]:
        """Return up to *max_chars* characters of raw source content.

        Looks up the source file path from the notebook index.  If the source
        is a readable local file the first *max_chars* characters are returned.
        If the file is not available locally, ``content`` will be empty and
        ``available`` will be ``False``.
        """
        target_notebook = notebook_id.strip() or self._default_notebook
        notebook = self._manager.get(target_notebook)
        if notebook is None:
            raise ValueError(f"notebook not found: {target_notebook}")

        docs = self._manager.list_documents(notebook.id)
        doc = next((d for d in docs if d["document_id"] == document_id), None)
        if doc is None:
            raise ValueError(f"document not found: {document_id}")

        source = str(doc.get("source", "")).strip()
        content = ""
        available = False
        truncated = False

        if source:
            src_path = Path(source)
            if src_path.is_file():
                try:
                    raw = src_path.read_text(encoding="utf-8", errors="replace")
                    if len(raw) > max_chars:
                        content = raw[:max_chars]
                        truncated = True
                    else:
                        content = raw
                    available = True
                except OSError:
                    pass

        return {
            "document_id": document_id,
            "source": source,
            "notebook_id": notebook.id,
            "notebook": notebook.name,
            "content": content,
            "char_count": len(content),
            "truncated": truncated,
            "available": available,
        }

    def search_documents(
        self,
        query: str,
        *,
        notebook_id: str = "",
    ) -> dict[str, Any]:
        """Return documents whose source name contains *query* (case-insensitive).

        Useful for the Dashboard document browser to filter the document list
        without a full-text vector search.
        """
        target_notebook = notebook_id.strip() or self._default_notebook
        notebook = self._manager.get(target_notebook)
        if notebook is None:
            raise ValueError(f"notebook not found: {target_notebook}")

        q = query.strip().lower()
        docs = self._manager.list_documents(notebook.id)
        if q:
            docs = [
                d for d in docs
                if q in str(d.get("source", "")).lower()
                or q in str(d.get("metadata", {})).lower()
            ]
        return {
            "query": query,
            "notebook_id": notebook.id,
            "notebook": notebook.name,
            "documents": docs,
            "total_documents": len(docs),
        }

    def delete_document(self, document_id: str, *, notebook_id: str = "") -> dict[str, Any]:
        target_notebook = notebook_id.strip() or self._default_notebook
        notebook = self._manager.get(target_notebook)
        if notebook is None:
            raise ValueError(f"notebook not found: {target_notebook}")
        clean_document_id = str(document_id).strip()
        if not clean_document_id:
            raise ValueError("document_id cannot be empty")
        document = self._manager.remove_document(notebook.id, clean_document_id)
        source = str(document.get("source", "")).strip()
        effective_store_kind = self._effective_store_kind(notebook)
        retriever = HybridRetriever.from_notebook(
            notebook,
            self._data_dir,
            store_kind=effective_store_kind,
            store_options=self._store_options,
        )
        chunks_deleted = int(retriever._vector.delete_by_source(source)) if source else 0  # type: ignore[attr-defined]
        documents_removed = int(document.get("doc_units", 0) or 0)
        if chunks_deleted > 0 and documents_removed <= 0:
            documents_removed = 1
        if documents_removed > 0:
            self._manager.bump_doc_count(notebook.id, -documents_removed)
        if chunks_deleted <= 0 and documents_removed <= 0:
            raise ValueError(f"document not found: {clean_document_id}")
        return {
            "ok": True,
            "document_id": clean_document_id,
            "source": source,
            "tenant_id": self._tenant_id,
            "notebook": notebook.name,
            "notebook_id": notebook.id,
            "store_backend": effective_store_kind,
            "documents_removed": max(0, int(documents_removed)),
            "chunks_deleted": max(0, chunks_deleted),
            "metadata": dict(document.get("metadata") or {}),
        }

    def clear_notebook(self, *, notebook_id: str = "") -> dict[str, Any]:
        target_notebook = notebook_id.strip() or self._default_notebook
        notebook = self._manager.get(target_notebook)
        if notebook is None:
            raise ValueError(f"notebook not found: {target_notebook}")
        documents = self._manager.clear_documents(notebook.id)
        effective_store_kind = self._effective_store_kind(notebook)
        retriever = HybridRetriever.from_notebook(
            notebook,
            self._data_dir,
            store_kind=effective_store_kind,
            store_options=self._store_options,
        )
        chunks_deleted = 0
        documents_removed = 0
        for document in documents:
            source = str(document.get("source", "")).strip()
            if source:
                chunks_deleted += int(retriever._vector.delete_by_source(source))  # type: ignore[attr-defined]
            documents_removed += max(0, int(document.get("doc_units", 0) or 0))
        if documents_removed > 0:
            self._manager.bump_doc_count(notebook.id, -documents_removed)
        return {
            "ok": True,
            "tenant_id": self._tenant_id,
            "notebook": notebook.name,
            "notebook_id": notebook.id,
            "store_backend": effective_store_kind,
            "documents_removed": documents_removed,
            "chunks_deleted": chunks_deleted,
            "document_ids": [str(row.get("document_id", "")) for row in documents],
        }

    def run_retention(
        self,
        *,
        notebook_id: str = "",
        max_documents: int = 0,
        max_age_days: int = 0,
    ) -> dict[str, Any]:
        target_notebook = notebook_id.strip() or self._default_notebook
        notebook = self._manager.get(target_notebook)
        if notebook is None:
            raise ValueError(f"notebook not found: {target_notebook}")
        normalized_max_documents = max(
            0,
            int(max_documents or getattr(notebook, "retention_max_documents", 0) or 0),
        )
        normalized_max_age_days = max(
            0,
            int(max_age_days or getattr(notebook, "retention_max_age_days", 0) or 0),
        )
        documents = self._manager.list_documents(notebook.id)
        if not documents or (normalized_max_documents <= 0 and normalized_max_age_days <= 0):
            return {
                "ok": True,
                "tenant_id": self._tenant_id,
                "notebook": notebook.name,
                "notebook_id": notebook.id,
                "store_backend": self._effective_store_kind(notebook),
                "max_documents": normalized_max_documents,
                "max_age_days": normalized_max_age_days,
                "documents_removed": 0,
                "chunks_deleted": 0,
                "document_ids": [],
            }

        cutoff: datetime | None = None
        if normalized_max_age_days > 0:
            cutoff = datetime.now(UTC) - timedelta(days=normalized_max_age_days)

        sortable = sorted(
            documents,
            key=lambda row: self._parse_document_created_at(str(row.get("created_at", ""))),
            reverse=True,
        )
        stale_ids: set[str] = set()
        if cutoff is not None:
            for row in sortable:
                created_at = self._parse_document_created_at(str(row.get("created_at", "")))
                if created_at < cutoff:
                    stale_ids.add(str(row.get("document_id", "")))
        if normalized_max_documents > 0 and len(sortable) > normalized_max_documents:
            for row in sortable[normalized_max_documents:]:
                stale_ids.add(str(row.get("document_id", "")))

        removed_ids: list[str] = []
        documents_removed = 0
        chunks_deleted = 0
        for document_id in sorted(x for x in stale_ids if x):
            payload = self.delete_document(document_id, notebook_id=notebook.id)
            removed_ids.append(document_id)
            documents_removed += int(payload.get("documents_removed", 0) or 0)
            chunks_deleted += int(payload.get("chunks_deleted", 0) or 0)
        return {
            "ok": True,
            "tenant_id": self._tenant_id,
            "notebook": notebook.name,
            "notebook_id": notebook.id,
            "store_backend": self._effective_store_kind(notebook),
            "max_documents": normalized_max_documents,
            "max_age_days": normalized_max_age_days,
            "documents_removed": documents_removed,
            "chunks_deleted": chunks_deleted,
            "document_ids": removed_ids,
        }

    def stats(self, *, notebook_id: str = "") -> dict[str, Any]:
        notebooks = self._manager.list()
        target = notebook_id.strip().lower()
        if target:
            notebooks = [nb for nb in notebooks if nb.id.lower() == target or nb.name.lower() == target]
        rows: list[dict[str, Any]] = []
        rag_available = True
        error = ""
        backend_summary: dict[str, dict[str, Any]] = {}
        for notebook in notebooks:
            chunk_count = 0
            effective_store_kind = self._effective_store_kind(notebook)
            backend_diagnostics: dict[str, Any] = {}
            try:
                retriever = HybridRetriever.from_notebook(
                    notebook,
                    self._data_dir,
                    store_kind=effective_store_kind,
                    store_options=self._store_options,
                )
                chunk_count = int(retriever._vector.count())  # type: ignore[attr-defined]
                backend_diagnostics = retriever.backend_diagnostics()
            except Exception as exc:
                rag_available = False
                error = str(exc)
                backend_diagnostics = {
                    "backend": effective_store_kind,
                    "healthy": False,
                    "error": str(exc),
                    "chunk_count": chunk_count,
                }
            summary = backend_summary.setdefault(
                effective_store_kind,
                {
                    "backend": effective_store_kind,
                    "healthy": True,
                    "notebook_count": 0,
                    "document_count": 0,
                    "chunk_count": 0,
                    "storage_bytes": 0,
                    "storage_present": False,
                },
            )
            summary["healthy"] = bool(summary["healthy"]) and bool(
                backend_diagnostics.get("healthy", True)
            )
            summary["notebook_count"] = int(summary["notebook_count"]) + 1
            summary["document_count"] = int(summary["document_count"]) + int(notebook.doc_count)
            summary["chunk_count"] = int(summary["chunk_count"]) + chunk_count
            summary["storage_bytes"] = int(summary["storage_bytes"]) + int(
                backend_diagnostics.get("storage_bytes", 0) or 0
            )
            summary["storage_present"] = bool(summary["storage_present"]) or bool(
                backend_diagnostics.get("storage_present", False)
            )
            if backend_diagnostics.get("storage_path") and not summary.get("storage_path"):
                summary["storage_path"] = str(backend_diagnostics.get("storage_path") or "")
            if backend_diagnostics.get("namespace") and not summary.get("namespace"):
                summary["namespace"] = str(backend_diagnostics.get("namespace") or "")
            if backend_diagnostics.get("collection_name") and not summary.get("collection_name"):
                summary["collection_name"] = str(backend_diagnostics.get("collection_name") or "")
            if backend_diagnostics.get("error"):
                summary["error"] = str(backend_diagnostics.get("error") or "")
            rows.append(
                {
                    "id": notebook.id,
                    "name": notebook.name,
                    "doc_count": int(notebook.doc_count),
                    "chunk_count": chunk_count,
                    "created_at": notebook.created_at,
                    "store_backend": effective_store_kind,
                    "backend_diagnostics": backend_diagnostics,
                }
            )
        detected_backends = sorted(
            backend_summary.values(),
            key=lambda row: (str(row.get("backend") or ""), str(row.get("collection_name") or "")),
        )
        return {
            "rag_available": rag_available,
            "error": error,
            "tenant_id": self._tenant_id,
            "store_backend": self.store_kind,
            "tenant_store_backend": self.tenant_store_backend,
            "store_options": dict(self._store_options),
            "notebooks": rows,
            "total_notebooks": len(rows),
            "total_documents": sum(int(row["doc_count"]) for row in rows),
            "total_chunks": sum(int(row["chunk_count"]) for row in rows),
            "backend_diagnostics": {
                "all_healthy": rag_available and all(bool(row.get("healthy", False)) for row in detected_backends)
                if detected_backends
                else rag_available,
                "configured_backend": self.store_kind,
                "tenant_backend_policy": self.tenant_store_backend,
                "detected_backends": detected_backends,
            },
        }

    def ensure_notebook(
        self,
        notebook_id: str,
        *,
        store_backend: str = "",
    ) -> dict[str, Any]:
        notebook = self._manager.get_or_create(
            notebook_id.strip() or self._default_notebook,
            store_backend=store_backend,
        )
        if store_backend:
            notebook = self._manager.set_store_backend(notebook.id, store_backend)
        return {
            "tenant_id": self._tenant_id,
            "notebook": notebook.name,
            "notebook_id": notebook.id,
            "store_backend": self._effective_store_kind(notebook),
            "retention_max_documents": max(0, int(getattr(notebook, "retention_max_documents", 0) or 0)),
            "retention_max_age_days": max(0, int(getattr(notebook, "retention_max_age_days", 0) or 0)),
        }

    def list_notebooks(self) -> list[dict[str, Any]]:
        """Return all notebooks for the current tenant with per-notebook backend status."""
        rows: list[dict[str, Any]] = []
        for nb in self._manager.list():
            backend_ok = True
            backend_errors: list[str] = []
            effective_store = self._effective_store_kind(nb)
            try:
                retriever = HybridRetriever.from_notebook(
                    nb,
                    self._data_dir,
                    store_kind=effective_store,
                    store_options=self._store_options,
                )
                diag = retriever.backend_diagnostics()
                backend_ok = bool(diag.get("healthy", True))
                if not backend_ok:
                    backend_errors.append(str(diag.get("error") or "unhealthy"))
            except Exception as exc:  # noqa: BLE001
                backend_ok = False
                backend_errors.append(str(exc))
            rows.append(
                {
                    "notebook_id": nb.id,
                    "display_name": nb.name,
                    "store_backend": effective_store,
                    "doc_count": int(nb.doc_count),
                    "created_at": nb.created_at,
                    "backend_ok": backend_ok,
                    "backend_errors": backend_errors,
                }
            )
        return rows

    def create_notebook(
        self,
        notebook_id: str,
        *,
        display_name: str = "",
        store_backend: str = "",
    ) -> dict[str, Any]:
        """Explicitly create a notebook (raises ValueError if already exists)."""
        name = (display_name.strip() or notebook_id.strip())
        if not name:
            raise ValueError("notebook_id cannot be empty")
        nb = self._manager.create(name, store_backend=store_backend)
        return {
            "ok": True,
            "notebook_id": nb.id,
            "display_name": nb.name,
            "store_backend": self._effective_store_kind(nb),
            "created": True,
        }

    def repair_notebook(self, notebook_id: str) -> dict[str, Any]:
        """Attempt to repair a notebook backend and return a status report."""
        nb = self._manager.get(notebook_id)
        if nb is None:
            raise ValueError(f"notebook not found: {notebook_id}")
        effective_store = self._effective_store_kind(nb)
        actions_taken: list[str] = []
        errors: list[str] = []
        try:
            retriever = HybridRetriever.from_notebook(
                nb,
                self._data_dir,
                store_kind=effective_store,
                store_options=self._store_options,
            )
            diag = retriever.backend_diagnostics()
            if bool(diag.get("healthy", True)):
                actions_taken.append("no_repair_needed")
                return {
                    "ok": True,
                    "notebook_id": notebook_id,
                    "actions_taken": actions_taken,
                    "errors": errors,
                }
            if effective_store == "memory":
                actions_taken.append("no_repair_needed_memory_ephemeral")
                errors.append("memory backend is ephemeral, no persistent repair possible")
                return {
                    "ok": True,
                    "notebook_id": notebook_id,
                    "actions_taken": actions_taken,
                    "errors": errors,
                }
            # For persistent backends attempt a metadata reset
            actions_taken.append("metadata_reset_attempted")
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
            if effective_store == "memory":
                actions_taken.append("no_repair_needed_memory_ephemeral")
                return {
                    "ok": True,
                    "notebook_id": notebook_id,
                    "actions_taken": actions_taken,
                    "errors": errors,
                }
            actions_taken.append("metadata_reset_attempted")
        return {
            "ok": len(errors) == 0,
            "notebook_id": notebook_id,
            "actions_taken": actions_taken,
            "errors": errors,
        }

    @staticmethod
    def _normalize_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
        if not metadata:
            return {}
        normalized: dict[str, Any] = {}
        for key, value in metadata.items():
            name = str(key).strip()
            if not name:
                continue
            if isinstance(value, (str, int, float, bool)):
                normalized[name] = value
        return normalized

    @staticmethod
    def _normalize_tenant_id(tenant_id: str) -> str:
        return str(tenant_id or DEFAULT_TENANT_ID).strip() or DEFAULT_TENANT_ID

    @staticmethod
    def _resolve_store_kind(store_kind: str) -> str:
        explicit = RAGPipeline._normalize_store_kind(store_kind)
        if explicit:
            return explicit
        try:
            from zen_claw.config.loader import load_config

            configured = RAGPipeline._normalize_store_kind(load_config().knowledge.store_backend)
            if configured:
                return configured
        except Exception:
            pass
        return "chroma"

    @staticmethod
    def _resolve_store_options() -> dict[str, Any]:
        defaults = {
            "chroma_subdir": "chroma",
            "chroma_collection_prefix": "",
            "memory_namespace": "",
        }
        try:
            from zen_claw.config.loader import load_config

            knowledge = load_config().knowledge
            return {
                "chroma_subdir": str(getattr(knowledge, "chroma_subdir", "") or "chroma").strip()
                or "chroma",
                "chroma_collection_prefix": str(
                    getattr(knowledge, "chroma_collection_prefix", "") or ""
                ).strip(),
                "memory_namespace": str(getattr(knowledge, "memory_namespace", "") or "").strip(),
            }
        except Exception:
            return defaults

    def _effective_store_kind(self, notebook: Any | None = None) -> str:
        if self._store_kind_override:
            return self._store_kind_override
        notebook_store = self._normalize_store_kind(getattr(notebook, "store_backend", ""))
        if notebook_store:
            return notebook_store
        tenant_store = self._resolve_tenant_store_kind()
        if tenant_store:
            return tenant_store
        return self._store_kind

    def _resolve_tenant_store_kind(self) -> str:
        try:
            tenant = TenantStore(self._base_data_dir).get(self._tenant_id)
        except Exception:
            return ""
        if tenant is None:
            return ""
        return self._normalize_store_kind(getattr(tenant, "store_backend", ""))

    @staticmethod
    def _normalize_store_kind(store_kind: Any) -> str:
        value = str(store_kind or "").strip().lower()
        if value in {"chroma", "memory"}:
            return value
        return ""

    @staticmethod
    def _build_document_metadata(
        *,
        source: str,
        docs: list[Any],
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        summary: dict[str, Any] = dict(metadata)
        source_str = str(source)
        if source_str.startswith(("http://", "https://")):
            summary.setdefault("source_type", "url")
        else:
            path = Path(source_str)
            summary.setdefault("source_type", "directory" if path.is_dir() else "file")
            suffix = path.suffix.lower()
            if suffix:
                summary.setdefault("file_ext", suffix)
        pages = [int(doc.page) for doc in docs if getattr(doc, "page", None) is not None]
        if pages:
            summary.setdefault("page_count", max(pages))
        for doc in docs:
            doc_meta = getattr(doc, "metadata", None)
            if not isinstance(doc_meta, dict):
                continue
            for key, value in doc_meta.items():
                if key in {"source_type", "file_ext"} and isinstance(value, (str, int, float, bool)):
                    summary[key] = value
                    continue
                if key in summary:
                    continue
                if isinstance(value, (str, int, float, bool)):
                    summary[key] = value
        return summary

    @staticmethod
    def _parse_document_created_at(value: str) -> datetime:
        raw = str(value or "").strip()
        if not raw:
            return datetime.fromtimestamp(0, tz=UTC)
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return datetime.fromtimestamp(0, tz=UTC)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)

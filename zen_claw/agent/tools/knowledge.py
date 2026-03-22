"""Knowledge base tools for RAG."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult
from zen_claw.auth.paths import DEFAULT_TENANT_ID
from zen_claw.knowledge.notebook import NotebookManager
from zen_claw.knowledge.pipeline import RAGPipeline


class KnowledgeListTool(Tool):
    name = "knowledge_list"
    description = "List available knowledge notebooks."
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, data_dir: Path, tenant_id: str = DEFAULT_TENANT_ID):
        self._pipeline = RAGPipeline(Path(data_dir), tenant_id=tenant_id)
        self._manager = NotebookManager(self._pipeline.data_dir)

    async def execute(self, **kwargs: Any) -> ToolResult:
        rows = [x.to_dict() for x in self._manager.list()]
        return ToolResult.success(json.dumps({"notebooks": rows}, ensure_ascii=False))


class KnowledgeSearchTool(Tool):
    name = "knowledge_search"
    description = "Search notebook knowledge with hybrid retrieval."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "notebook_id": {"type": "string"},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
            "filters": {"type": "object"},
            "tenant_id": {"type": "string"},
            "store_backend": {"type": "string", "enum": ["chroma", "memory"]},
        },
        "required": ["query"],
    }

    def __init__(
        self,
        data_dir: Path,
        default_notebook: str = "default",
        tenant_id: str = DEFAULT_TENANT_ID,
        store_kind: str = "",
    ):
        self._data_dir = Path(data_dir)
        self._pipeline = RAGPipeline(
            self._data_dir,
            default_notebook=default_notebook,
            tenant_id=tenant_id,
            store_kind=store_kind,
        )
        self._manager = NotebookManager(self._pipeline.data_dir)
        self._default_notebook = default_notebook

    async def execute(
        self,
        query: str,
        notebook_id: str = "",
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
        tenant_id: str = "",
        store_backend: str = "",
        **kwargs: Any,
    ) -> ToolResult:
        backend_override = str(store_backend or "").strip().lower()
        tenant_override = str(tenant_id or "").strip()
        if (
            tenant_override and tenant_override != self._pipeline.tenant_id
        ) or (backend_override and backend_override != self._pipeline.store_kind):
            self._pipeline = RAGPipeline(
                self._data_dir,
                default_notebook=self._default_notebook,
                tenant_id=tenant_override,
                store_kind=backend_override,
            )
            self._manager = NotebookManager(self._pipeline.data_dir)
        nb_name = notebook_id or self._default_notebook
        nb = self._manager.get(nb_name)
        if not nb:
            return ToolResult.failure(
                ToolErrorKind.PARAMETER, f"notebook not found: {nb_name}", code="notebook_not_found"
            )
        try:
            payload = self._pipeline.search(
                query=query,
                notebook_id=nb_name,
                top_k=top_k,
                filters=filters,
            )
        except Exception as exc:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                f"knowledge search failed: {exc}",
                code="knowledge_search_failed",
            )
        return ToolResult.success(json.dumps(payload, ensure_ascii=False))


class KnowledgeAddTool(Tool):
    name = "knowledge_add"
    description = "Ingest a file/URL into notebook knowledge base."
    parameters = {
        "type": "object",
        "properties": {
            "source": {"type": "string"},
            "notebook_id": {"type": "string"},
            "metadata": {"type": "object"},
            "tenant_id": {"type": "string"},
            "store_backend": {"type": "string", "enum": ["chroma", "memory"]},
        },
        "required": ["source"],
    }

    def __init__(
        self, data_dir: Path, tenant_id: str = DEFAULT_TENANT_ID, store_kind: str = ""
    ):
        self._data_dir = Path(data_dir)
        self._pipeline = RAGPipeline(self._data_dir, tenant_id=tenant_id, store_kind=store_kind)

    async def execute(
        self,
        source: str,
        notebook_id: str = "default",
        metadata: dict[str, Any] | None = None,
        tenant_id: str = "",
        store_backend: str = "",
        **kwargs: Any,
    ) -> ToolResult:
        backend_override = str(store_backend or "").strip().lower()
        tenant_override = str(tenant_id or "").strip()
        if (
            tenant_override and tenant_override != self._pipeline.tenant_id
        ) or (backend_override and backend_override != self._pipeline.store_kind):
            self._pipeline = RAGPipeline(
                self._data_dir, tenant_id=tenant_override, store_kind=backend_override
            )
        try:
            payload = await self._pipeline.ingest_with_metadata(
                source,
                notebook_id=notebook_id or "default",
                metadata=metadata,
            )
        except Exception as exc:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                f"knowledge ingest failed: {exc}",
                code="knowledge_add_failed",
            )
        return ToolResult.success(json.dumps(payload, ensure_ascii=False))

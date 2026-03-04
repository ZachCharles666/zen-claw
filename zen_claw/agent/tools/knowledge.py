"""Knowledge base tools for RAG."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult
from zen_claw.knowledge.ingestor import Ingestor
from zen_claw.knowledge.notebook import NotebookManager
from zen_claw.knowledge.retriever import HybridRetriever


class KnowledgeListTool(Tool):
    name = "knowledge_list"
    description = "List available knowledge notebooks."
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, data_dir: Path):
        self._manager = NotebookManager(Path(data_dir))

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
        },
        "required": ["query"],
    }

    def __init__(self, data_dir: Path, default_notebook: str = "default"):
        self._data_dir = Path(data_dir)
        self._manager = NotebookManager(self._data_dir)
        self._default_notebook = default_notebook

    async def execute(self, query: str, notebook_id: str = "", top_k: int = 5, **kwargs: Any) -> ToolResult:
        nb_name = notebook_id or self._default_notebook
        nb = self._manager.get(nb_name)
        if not nb:
            return ToolResult.failure(ToolErrorKind.PARAMETER, f"notebook not found: {nb_name}", code="notebook_not_found")
        try:
            retriever = HybridRetriever.from_notebook(nb, self._data_dir)
            results = retriever.search(query=query, top_k=top_k)
        except Exception as exc:
            return ToolResult.failure(ToolErrorKind.RUNTIME, f"knowledge search failed: {exc}", code="knowledge_search_failed")
        payload = {
            "notebook": nb.name,
            "query": query,
            "results": [
                {"content": r.content, "source": r.source, "score": float(r.rrf_score or r.score), "page": r.page}
                for r in results
            ],
        }
        return ToolResult.success(json.dumps(payload, ensure_ascii=False))


class KnowledgeAddTool(Tool):
    name = "knowledge_add"
    description = "Ingest a file/URL into notebook knowledge base."
    parameters = {
        "type": "object",
        "properties": {"source": {"type": "string"}, "notebook_id": {"type": "string"}},
        "required": ["source"],
    }

    def __init__(self, data_dir: Path):
        self._data_dir = Path(data_dir)
        self._manager = NotebookManager(self._data_dir)
        self._ingestor = Ingestor()

    async def execute(self, source: str, notebook_id: str = "default", **kwargs: Any) -> ToolResult:
        nb = self._manager.get_or_create(notebook_id or "default")
        try:
            docs = await self._ingestor.ingest(source)
            retriever = HybridRetriever.from_notebook(nb, self._data_dir)
            chunks = await retriever.add_documents(docs)
        except Exception as exc:
            return ToolResult.failure(ToolErrorKind.RUNTIME, f"knowledge ingest failed: {exc}", code="knowledge_add_failed")
        self._manager.bump_doc_count(nb.id, max(1, len(docs)))
        return ToolResult.success(
            json.dumps(
                {"notebook": nb.name, "source": source, "documents": len(docs), "chunks_added": int(chunks)},
                ensure_ascii=False,
            )
        )

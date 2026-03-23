"""Tests for RAG foundation."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from zen_claw.knowledge.chunker import ChunkConfig, TextChunker, _hard_split


def test_chunker_short_text_returns_single_chunk():
    chunker = TextChunker(ChunkConfig(max_chars=800))
    chunks = chunker.chunk("This is a short sentence.")
    assert len(chunks) == 1
    assert chunks[0] == "This is a short sentence."


def test_chunker_long_text_splits_into_multiple():
    chunker = TextChunker(ChunkConfig(max_chars=100, overlap_chars=20, min_chars=10))
    text = " ".join([f"This is sentence number {i}." for i in range(20)])
    chunks = chunker.chunk(text)
    assert len(chunks) > 1
    assert all(len(c) <= 300 for c in chunks)


def test_chunker_overlap_present():
    chunker = TextChunker(ChunkConfig(max_chars=80, overlap_chars=30, min_chars=5))
    text = "First paragraph has content.\n\nSecond paragraph has more content."
    chunks = chunker.chunk(text)
    if len(chunks) >= 2:
        tail = chunks[0][-30:]
        assert any(tok in chunks[1] for tok in tail.split())


def test_chunker_min_chars_filters_tiny_chunks():
    chunker = TextChunker(ChunkConfig(max_chars=800, min_chars=50))
    text = "Hi.\n\nThis is a much longer paragraph that definitely satisfies minimum chunk size."
    chunks = chunker.chunk(text)
    assert all(len(c) >= 50 or len(chunks) == 1 for c in chunks)


def test_chunker_empty_text_returns_empty():
    chunker = TextChunker()
    assert chunker.chunk("") == []
    assert chunker.chunk("   \n ") == []


def test_chunker_chinese_text():
    chunker = TextChunker(ChunkConfig(max_chars=50, min_chars=5))
    text = "这是第一句话。这是第二句话。这是第三句话，后面还有更多内容！这是第四句话。"
    chunks = chunker.chunk(text)
    assert len(chunks) >= 1
    assert all(c.strip() for c in chunks)


def test_chunker_with_metadata():
    chunker = TextChunker()
    rows = chunker.chunk_with_metadata(
        "Some content.\n\nMore content.",
        source="x.txt",
        page=1,
        metadata={"industry": "finance"},
    )
    assert isinstance(rows, list)
    for row in rows:
        assert row["source"] == "x.txt"
        assert row["page"] == 1
        assert "chunk_index" in row
        assert row["metadata"]["industry"] == "finance"


def test_hard_split_returns_overlapping_chunks():
    chunks = _hard_split("a" * 200, max_chars=100, overlap=20)
    assert len(chunks) >= 2
    assert len(chunks[0]) == 100
    assert all(len(c) <= 100 for c in chunks)


async def test_ingestor_txt_file(tmp_path: Path):
    from zen_claw.knowledge.ingestor import Ingestor

    txt = tmp_path / "test.txt"
    txt.write_text("Hello world", encoding="utf-8")
    docs = await Ingestor().ingest(str(txt))
    assert len(docs) == 1
    assert docs[0].source == str(txt)
    assert "Hello" in docs[0].content


async def test_ingestor_directory_collects_supported_files(tmp_path: Path):
    from zen_claw.knowledge.ingestor import Ingestor

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "a.txt").write_text("Alpha", encoding="utf-8")
    (docs_dir / "b.md").write_text("# Beta", encoding="utf-8")
    (docs_dir / "skip.xyz").write_text("ignored", encoding="utf-8")
    nested = docs_dir / "nested"
    nested.mkdir()
    (nested / "c.html").write_text("<html><body>Gamma</body></html>", encoding="utf-8")

    docs = await Ingestor().ingest(str(docs_dir))

    assert len(docs) == 3
    sources = {Path(doc.source).name for doc in docs}
    assert sources == {"a.txt", "b.md", "c.html"}


async def test_ingestor_directory_without_supported_files_fails(tmp_path: Path):
    from zen_claw.knowledge.ingestor import Ingestor

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "skip.xyz").write_text("ignored", encoding="utf-8")

    with pytest.raises(ValueError, match="No supported files found in directory"):
        await Ingestor().ingest(str(docs_dir))


async def test_ingestor_unsupported_extension():
    from zen_claw.knowledge.ingestor import Ingestor

    with pytest.raises(ValueError, match="Unsupported file type"):
        await Ingestor().ingest("/x/file.xyz")


try:
    import chromadb  # noqa: F401

    HAS_CHROMADB = True
except ImportError:
    HAS_CHROMADB = False


@pytest.mark.skipif(not HAS_CHROMADB, reason="chromadb not installed")
def test_chroma_store_add_and_search(tmp_path: Path):
    from zen_claw.knowledge.store import ChromaStore

    mock_embedder = MagicMock()
    mock_embedder.embed.side_effect = lambda texts: [[0.1, 0.2, 0.3] for _ in texts]
    store = ChromaStore("test_collection", tmp_path, embedder=mock_embedder)
    store.add(
        [
            {"content": "Python is a programming language.", "source": "a.txt", "page": 1},
            {"content": "Machine learning uses data.", "source": "a.txt", "page": 2},
            {"content": "Chroma stores vectors.", "source": "b.txt", "page": None},
        ]
    )
    assert store.count() == 3
    results = store.search("Python programming", top_k=2)
    assert len(results) >= 1
    assert 0.0 <= results[0].score <= 1.0


@pytest.mark.skipif(not HAS_CHROMADB, reason="chromadb not installed")
def test_chroma_store_delete_by_source(tmp_path: Path):
    from zen_claw.knowledge.store import ChromaStore

    mock_embedder = MagicMock()
    mock_embedder.embed.side_effect = lambda texts: [[0.5, 0.5, 0.5] for _ in texts]
    store = ChromaStore("test_delete", tmp_path, embedder=mock_embedder)
    store.add(
        [
            {"content": "From source A", "source": "a.txt"},
            {"content": "Also from A", "source": "a.txt"},
            {"content": "From source B", "source": "b.txt"},
        ]
    )
    assert store.count() == 3
    assert store.delete_by_source("a.txt") == 2
    assert store.count() == 1


async def test_knowledge_search_tool_notebook_not_found(tmp_path: Path):
    from zen_claw.agent.tools.knowledge import KnowledgeSearchTool

    tool = KnowledgeSearchTool(data_dir=tmp_path)
    result = await tool.execute(query="hello", notebook_id="missing")
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "notebook_not_found"


@pytest.mark.skipif(not HAS_CHROMADB, reason="chromadb not installed")
async def test_knowledge_search_tool_returns_results(tmp_path: Path):
    from zen_claw.agent.tools.knowledge import KnowledgeSearchTool
    from zen_claw.auth.paths import tenant_data_dir
    from zen_claw.knowledge.notebook import NotebookManager
    from zen_claw.knowledge.retriever import HybridSearchResult

    manager = NotebookManager(tenant_data_dir(tmp_path, "default"))
    manager.create("test_notebook")
    with patch("zen_claw.knowledge.retriever.HybridRetriever.from_notebook") as mock_from_nb:
        mock_retriever = MagicMock()
        mock_retriever.search.return_value = [
            HybridSearchResult(
                content="The sky is blue.",
                source="facts.txt",
                score=0.95,
                page=None,
                rrf_score=0.95,
            )
        ]
        mock_from_nb.return_value = mock_retriever
        tool = KnowledgeSearchTool(data_dir=tmp_path, default_notebook="test_notebook")
        result = await tool.execute(query="color of sky", notebook_id="test_notebook")

    assert result.ok is True
    data = json.loads(result.content)
    assert len(data["results"]) == 1
    assert data["results"][0]["score"] == 0.95


async def test_rag_pipeline_ingest_and_stats_with_mocked_retriever(tmp_path: Path):
    from zen_claw.knowledge.pipeline import RAGPipeline

    txt = tmp_path / "doc.txt"
    txt.write_text("Hello knowledge base", encoding="utf-8")

    class _FakeVector:
        def count(self) -> int:
            return 3

        def diagnostics(self) -> dict[str, object]:
            return {
                "backend": "memory",
                "healthy": True,
                "namespace": "tenant-cache",
                "storage_bytes": 0,
                "storage_present": False,
                "chunk_count": 3,
            }

    class _FakeRetriever:
        def __init__(self):
            self._vector = _FakeVector()
            self.seen_docs = []

        async def add_documents(self, docs):
            self.seen_docs = docs
            assert len(docs) == 1
            return 3

        def backend_diagnostics(self) -> dict[str, object]:
            return self._vector.diagnostics()

    fake = _FakeRetriever()
    with patch("zen_claw.knowledge.retriever.HybridRetriever.from_notebook", return_value=fake):
        pipeline = RAGPipeline(tmp_path, store_kind="memory")
        payload = await pipeline.ingest_with_metadata(
            str(txt), notebook_id="kb", metadata={"industry": "finance", "tier": 1}
        )
        stats = pipeline.stats(notebook_id="kb")

    assert payload["notebook"] == "kb"
    assert payload["documents"] == 1
    assert payload["chunks_added"] == 3
    assert payload["metadata"]["industry"] == "finance"
    assert fake.seen_docs[0].metadata["industry"] == "finance"
    assert stats["total_notebooks"] == 1
    assert stats["total_documents"] == 1
    assert stats["total_chunks"] == 3
    assert stats["backend_diagnostics"]["all_healthy"] is True
    assert stats["backend_diagnostics"]["detected_backends"][0]["backend"] == "memory"
    assert stats["notebooks"][0]["backend_diagnostics"]["namespace"] == "tenant-cache"


async def test_rag_pipeline_ingest_documents_accepts_prebuilt_docs(tmp_path: Path) -> None:
    from zen_claw.knowledge.ingestor import Document
    from zen_claw.knowledge.pipeline import RAGPipeline

    pipeline = RAGPipeline(tmp_path, store_kind="memory")
    payload = await pipeline.ingest_documents(
        [
            Document(
                content="Crawler body for finance updates",
                source="https://example.com/news",
                metadata={"topic": "finance", "source_type": "browser"},
            )
        ],
        notebook_id="crawler_kb",
        source="https://example.com/news",
    )
    docs_payload = pipeline.list_documents(notebook_id="crawler_kb")

    assert payload["documents"] == 1
    assert payload["source"] == "https://example.com/news"
    assert payload["chunks_added"] >= 1
    assert docs_payload["documents"][0]["source"] == "https://example.com/news"
    assert docs_payload["documents"][0]["metadata"]["topic"] == "finance"
    assert docs_payload["documents"][0]["metadata"]["source_type"] == "browser"


def test_rag_pipeline_search_filters_by_source(tmp_path: Path):
    from zen_claw.auth.paths import tenant_data_dir
    from zen_claw.knowledge.notebook import NotebookManager
    from zen_claw.knowledge.pipeline import RAGPipeline
    from zen_claw.knowledge.retriever import HybridSearchResult

    manager = NotebookManager(tenant_data_dir(tmp_path, "default"))
    manager.create("kb")
    with patch("zen_claw.knowledge.retriever.HybridRetriever.from_notebook") as mock_from_nb:
        mock_retriever = MagicMock()
        mock_retriever.search.return_value = [
            HybridSearchResult(
                content="Finance policy text",
                source="finance_policy.md",
                score=0.9,
                page=1,
                rrf_score=0.95,
            ),
            HybridSearchResult(
                content="Retail FAQ",
                source="retail.md",
                score=0.5,
                page=None,
                rrf_score=0.5,
            ),
        ]
        mock_from_nb.return_value = mock_retriever
        payload = RAGPipeline(tmp_path).search(
            "policy", notebook_id="kb", top_k=5, source_contains="finance"
        )

    assert payload["notebook"] == "kb"
    assert len(payload["results"]) == 1
    assert payload["results"][0]["source"] == "finance_policy.md"


def test_store_build_meta_merges_custom_metadata() -> None:
    from zen_claw.knowledge.store import _build_meta

    meta = _build_meta(
        {
            "source": "doc.txt",
            "page": 1,
            "chunk_index": 0,
            "metadata": {"industry": "finance", "tenant_id": "tenant-a"},
        }
    )
    assert meta["source"] == "doc.txt"
    assert meta["industry"] == "finance"
    assert meta["tenant_id"] == "tenant-a"


def test_create_vector_store_memory_add_search_and_delete(tmp_path: Path) -> None:
    from zen_claw.knowledge.store import InMemoryStore, create_vector_store

    store = create_vector_store("kb", tmp_path, store_kind="memory")
    assert isinstance(store, InMemoryStore)

    store.add(
        [
            {
                "content": "Finance onboarding policy",
                "source": "finance.md",
                "page": 1,
                "metadata": {"tenant_id": "tenant-a", "category": "policy"},
            },
            {
                "content": "Retail FAQ",
                "source": "retail.md",
                "page": 2,
                "metadata": {"tenant_id": "tenant-b", "category": "faq"},
            },
        ]
    )

    assert store.count() == 2
    filtered = store.search("policy", top_k=5, filters={"tenant_id": "tenant-a"})
    assert len(filtered) == 1
    assert filtered[0].source == "finance.md"
    assert filtered[0].metadata == {"tenant_id": "tenant-a", "category": "policy"}

    assert store.delete_by_source("finance.md") == 1
    assert store.count() == 1


def test_create_vector_store_memory_namespace_isolated(tmp_path: Path) -> None:
    from zen_claw.knowledge.store import create_vector_store

    store_a = create_vector_store(
        "kb",
        tmp_path,
        store_kind="memory",
        store_options={"memory_namespace": "tenant-a"},
    )
    store_b = create_vector_store(
        "kb",
        tmp_path,
        store_kind="memory",
        store_options={"memory_namespace": "tenant-b"},
    )

    store_a.add([{"content": "Alpha policy", "source": "a.txt"}])

    assert store_a.count() == 1
    assert store_b.count() == 0


def test_create_vector_store_rejects_unknown_store_kind(tmp_path: Path) -> None:
    from zen_claw.knowledge.store import create_vector_store

    with pytest.raises(ValueError, match="unsupported vector store"):
        create_vector_store("kb", tmp_path, store_kind="unknown")


async def test_rag_pipeline_memory_store_persists_across_ingest_and_search(tmp_path: Path) -> None:
    from zen_claw.knowledge.pipeline import RAGPipeline

    txt = tmp_path / "policy.txt"
    txt.write_text("Finance onboarding policy for tenant alpha.", encoding="utf-8")

    pipeline = RAGPipeline(tmp_path, store_kind="memory")
    ingest_payload = await pipeline.ingest_with_metadata(
        str(txt),
        notebook_id="kb",
        metadata={"tenant_id": "tenant-a", "category": "policy"},
    )
    search_payload = pipeline.search(
        "onboarding",
        notebook_id="kb",
        top_k=5,
        filters={"tenant_id": "tenant-a"},
    )
    stats_payload = pipeline.stats(notebook_id="kb")

    assert ingest_payload["chunks_added"] >= 1
    assert len(search_payload["results"]) == 1
    assert search_payload["results"][0]["source"].endswith("policy.txt")
    assert stats_payload["total_chunks"] >= 1
    assert stats_payload["backend_diagnostics"]["detected_backends"][0]["backend"] == "memory"
    assert stats_payload["backend_diagnostics"]["detected_backends"][0]["notebook_count"] == 1
    assert stats_payload["notebooks"][0]["backend_diagnostics"]["healthy"] is True


def test_rag_pipeline_uses_tenant_scoped_data_dir(tmp_path: Path) -> None:
    from zen_claw.knowledge.pipeline import RAGPipeline

    tenant_id = str(uuid.uuid4())
    pipeline = RAGPipeline(tmp_path, tenant_id=tenant_id, store_kind="memory")

    assert "tenants" in str(pipeline.data_dir)
    assert tenant_id in str(pipeline.data_dir)
    assert pipeline.tenant_id == tenant_id


def test_rag_pipeline_uses_configured_store_backend_when_not_explicit(tmp_path: Path, monkeypatch) -> None:
    from zen_claw.knowledge.pipeline import RAGPipeline

    class _FakeKnowledge:
        store_backend = "memory"
        chroma_subdir = "rag-chroma"
        chroma_collection_prefix = "prod_"
        memory_namespace = "tenant-cache"

    class _FakeConfig:
        knowledge = _FakeKnowledge()

    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda: _FakeConfig())

    pipeline = RAGPipeline(tmp_path)
    assert pipeline.store_kind == "memory"
    assert pipeline.store_options == {
        "chroma_subdir": "rag-chroma",
        "chroma_collection_prefix": "prod_",
        "memory_namespace": "tenant-cache",
    }


def test_notebook_manager_persists_store_backend_policy(tmp_path: Path) -> None:
    from zen_claw.knowledge.notebook import NotebookManager

    manager = NotebookManager(tmp_path)
    created = manager.create("finance", store_backend="memory")
    updated = manager.set_store_backend("finance", "chroma")
    listed = manager.list()

    assert created.store_backend == "memory"
    assert updated.store_backend == "chroma"
    assert listed[0].store_backend == "chroma"


async def test_rag_pipeline_prefers_notebook_store_backend_over_config_default(
    tmp_path: Path, monkeypatch
) -> None:
    from zen_claw.knowledge.pipeline import RAGPipeline

    class _FakeKnowledge:
        store_backend = "chroma"

    class _FakeConfig:
        knowledge = _FakeKnowledge()

    seen_store_kinds: list[str] = []

    class _FakeVector:
        def count(self) -> int:
            return 0

    class _FakeRetriever:
        def __init__(self):
            self._vector = _FakeVector()

        async def add_documents(self, docs):
            return len(docs)

    def _fake_from_notebook(notebook, data_dir, store_kind="", store_options=None):
        seen_store_kinds.append(store_kind)
        assert store_options == {
            "chroma_subdir": "chroma",
            "chroma_collection_prefix": "",
            "memory_namespace": "",
        }
        return _FakeRetriever()

    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda: _FakeConfig())
    monkeypatch.setattr(
        "zen_claw.knowledge.retriever.HybridRetriever.from_notebook",
        _fake_from_notebook,
    )

    txt = tmp_path / "policy.txt"
    txt.write_text("Notebook specific backend policy.", encoding="utf-8")

    pipeline = RAGPipeline(tmp_path)
    pipeline.ensure_notebook("finance", store_backend="memory")
    payload = await pipeline.ingest(str(txt), notebook_id="finance")

    assert payload["store_backend"] == "memory"
    assert seen_store_kinds == ["memory"]


async def test_rag_pipeline_explicit_store_backend_overrides_notebook_policy(tmp_path: Path) -> None:
    from zen_claw.knowledge.pipeline import RAGPipeline

    seen_store_kinds: list[str] = []

    class _FakeVector:
        def count(self) -> int:
            return 0

    class _FakeRetriever:
        def __init__(self):
            self._vector = _FakeVector()

        async def add_documents(self, docs):
            return len(docs)

    def _fake_from_notebook(notebook, data_dir, store_kind="", store_options=None):
        seen_store_kinds.append(store_kind)
        return _FakeRetriever()

    with patch(
        "zen_claw.knowledge.retriever.HybridRetriever.from_notebook",
        _fake_from_notebook,
    ):
        manager_dir = tmp_path / "tenants" / "default"
        from zen_claw.knowledge.notebook import NotebookManager

        manager = NotebookManager(manager_dir)
        manager.create("finance", store_backend="memory")
        txt = tmp_path / "policy.txt"
        txt.write_text("Explicit override wins.", encoding="utf-8")
        payload = await RAGPipeline(tmp_path, store_kind="chroma").ingest(
            str(txt), notebook_id="finance"
        )

    assert payload["store_backend"] == "chroma"
    assert seen_store_kinds == ["chroma"]


async def test_rag_pipeline_prefers_tenant_store_backend_over_config_default(
    tmp_path: Path, monkeypatch
) -> None:
    from zen_claw.auth.tenant import TenantStore
    from zen_claw.knowledge.pipeline import RAGPipeline

    class _FakeKnowledge:
        store_backend = "chroma"

    class _FakeConfig:
        knowledge = _FakeKnowledge()

    tenant = TenantStore(tmp_path).create("Acme", store_backend="memory")
    seen_store_kinds: list[str] = []

    class _FakeVector:
        def count(self) -> int:
            return 0

    class _FakeRetriever:
        def __init__(self):
            self._vector = _FakeVector()

        async def add_documents(self, docs):
            return len(docs)

    def _fake_from_notebook(notebook, data_dir, store_kind="", store_options=None):
        seen_store_kinds.append(store_kind)
        return _FakeRetriever()

    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda: _FakeConfig())
    monkeypatch.setattr(
        "zen_claw.knowledge.retriever.HybridRetriever.from_notebook",
        _fake_from_notebook,
    )

    txt = tmp_path / "tenant-policy.txt"
    txt.write_text("Tenant specific backend policy.", encoding="utf-8")

    payload = await RAGPipeline(tmp_path, tenant_id=tenant.tenant_id).ingest(
        str(txt), notebook_id="finance"
    )

    assert payload["store_backend"] == "memory"
    assert seen_store_kinds == ["memory"]


async def test_rag_pipeline_passes_configured_store_options_to_retriever(
    tmp_path: Path, monkeypatch
) -> None:
    from zen_claw.knowledge.pipeline import RAGPipeline

    class _FakeKnowledge:
        store_backend = "memory"
        chroma_subdir = "rag-chroma"
        chroma_collection_prefix = "prod_"
        memory_namespace = "tenant-cache"

    class _FakeConfig:
        knowledge = _FakeKnowledge()

    seen_options: list[dict[str, object] | None] = []

    class _FakeVector:
        def count(self) -> int:
            return 0

    class _FakeRetriever:
        def __init__(self):
            self._vector = _FakeVector()

        async def add_documents(self, docs):
            return len(docs)

    def _fake_from_notebook(notebook, data_dir, store_kind="", store_options=None):
        seen_options.append(dict(store_options or {}))
        return _FakeRetriever()

    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda: _FakeConfig())
    monkeypatch.setattr(
        "zen_claw.knowledge.retriever.HybridRetriever.from_notebook",
        _fake_from_notebook,
    )

    txt = tmp_path / "store-options.txt"
    txt.write_text("Backend-specific config passthrough.", encoding="utf-8")

    await RAGPipeline(tmp_path).ingest(str(txt), notebook_id="finance")

    assert seen_options == [
        {
            "chroma_subdir": "rag-chroma",
            "chroma_collection_prefix": "prod_",
            "memory_namespace": "tenant-cache",
        }
    ]


async def test_rag_pipeline_notebook_policy_overrides_tenant_policy(tmp_path: Path) -> None:
    from zen_claw.auth.tenant import TenantStore
    from zen_claw.knowledge.pipeline import RAGPipeline

    tenant = TenantStore(tmp_path).create("Acme", store_backend="chroma")
    seen_store_kinds: list[str] = []

    class _FakeVector:
        def count(self) -> int:
            return 0

    class _FakeRetriever:
        def __init__(self):
            self._vector = _FakeVector()

        async def add_documents(self, docs):
            return len(docs)

    def _fake_from_notebook(notebook, data_dir, store_kind="", store_options=None):
        seen_store_kinds.append(store_kind)
        return _FakeRetriever()

    with patch(
        "zen_claw.knowledge.retriever.HybridRetriever.from_notebook",
        _fake_from_notebook,
    ):
        txt = tmp_path / "notebook-policy.txt"
        txt.write_text("Notebook wins over tenant policy.", encoding="utf-8")
        pipeline = RAGPipeline(tmp_path, tenant_id=tenant.tenant_id)
        pipeline.ensure_notebook("finance", store_backend="memory")
        payload = await pipeline.ingest(str(txt), notebook_id="finance")

    assert payload["store_backend"] == "memory"
    assert seen_store_kinds == ["memory"]


async def test_rag_pipeline_delete_document_removes_source_and_updates_stats(tmp_path: Path) -> None:
    from zen_claw.knowledge.pipeline import RAGPipeline

    txt = tmp_path / "remove-me.txt"
    txt.write_text("Delete this policy document from the notebook.", encoding="utf-8")

    pipeline = RAGPipeline(tmp_path, store_kind="memory")
    ingest_payload = await pipeline.ingest(str(txt), notebook_id="kb")
    before = pipeline.stats(notebook_id="kb")
    docs_payload = pipeline.list_documents(notebook_id="kb")
    payload = pipeline.delete_document(ingest_payload["document_id"], notebook_id="kb")
    after = pipeline.stats(notebook_id="kb")

    assert before["total_documents"] == 1
    assert docs_payload["total_documents"] == 1
    assert docs_payload["documents"][0]["document_id"] == ingest_payload["document_id"]
    assert docs_payload["documents"][0]["source"] == str(txt)
    assert docs_payload["documents"][0]["metadata"]["source_type"] == "file"
    assert docs_payload["documents"][0]["metadata"]["file_ext"] == ".txt"
    assert payload["documents_removed"] == 1
    assert payload["chunks_deleted"] >= 1
    assert payload["metadata"]["source_type"] == "file"
    assert after["total_documents"] == 0
    assert after["total_chunks"] == 0


def test_rag_pipeline_delete_document_not_found_raises(tmp_path: Path) -> None:
    from zen_claw.auth.paths import tenant_data_dir
    from zen_claw.knowledge.notebook import NotebookManager
    from zen_claw.knowledge.pipeline import RAGPipeline

    NotebookManager(tenant_data_dir(tmp_path, "default")).create("kb")
    pipeline = RAGPipeline(tmp_path, store_kind="memory")

    with pytest.raises(ValueError, match="document not found"):
        pipeline.delete_document("missing-doc-id", notebook_id="kb")


async def test_rag_pipeline_clear_notebook_removes_all_documents_and_keeps_notebook(tmp_path: Path) -> None:
    from zen_claw.knowledge.pipeline import RAGPipeline

    doc_a = tmp_path / "a.txt"
    doc_b = tmp_path / "b.txt"
    doc_a.write_text("Alpha policy", encoding="utf-8")
    doc_b.write_text("Beta policy", encoding="utf-8")

    pipeline = RAGPipeline(tmp_path, store_kind="memory")
    await pipeline.ingest(str(doc_a), notebook_id="kb")
    await pipeline.ingest(str(doc_b), notebook_id="kb")

    before_documents = pipeline.list_documents(notebook_id="kb")
    clear_payload = pipeline.clear_notebook(notebook_id="kb")
    after_documents = pipeline.list_documents(notebook_id="kb")
    after_stats = pipeline.stats(notebook_id="kb")

    assert before_documents["total_documents"] == 2
    assert clear_payload["documents_removed"] == 2
    assert clear_payload["chunks_deleted"] >= 2
    assert len(clear_payload["document_ids"]) == 2
    assert after_documents["total_documents"] == 0
    assert after_stats["total_documents"] == 0
    assert after_stats["notebooks"][0]["name"] == "kb"


async def test_rag_pipeline_run_retention_removes_older_documents(tmp_path: Path) -> None:
    from zen_claw.knowledge.pipeline import RAGPipeline

    doc_a = tmp_path / "a.txt"
    doc_b = tmp_path / "b.txt"
    doc_a.write_text("Alpha policy", encoding="utf-8")
    doc_b.write_text("Beta policy", encoding="utf-8")

    pipeline = RAGPipeline(tmp_path, store_kind="memory")
    first = await pipeline.ingest(str(doc_a), notebook_id="kb")
    second = await pipeline.ingest(str(doc_b), notebook_id="kb")

    retained = pipeline.run_retention(notebook_id="kb", max_documents=1, max_age_days=0)
    remaining = pipeline.list_documents(notebook_id="kb")

    assert retained["documents_removed"] == 1
    assert first["document_id"] in retained["document_ids"]
    assert second["document_id"] not in retained["document_ids"]
    assert remaining["total_documents"] == 1


async def test_rag_pipeline_run_retention_returns_zero_when_policy_disabled(tmp_path: Path) -> None:
    from zen_claw.knowledge.pipeline import RAGPipeline

    doc_a = tmp_path / "a.txt"
    doc_a.write_text("Alpha policy", encoding="utf-8")

    pipeline = RAGPipeline(tmp_path, store_kind="memory")
    await pipeline.ingest(str(doc_a), notebook_id="kb")

    retained = pipeline.run_retention(notebook_id="kb", max_documents=0, max_age_days=0)
    assert retained["documents_removed"] == 0
    assert retained["document_ids"] == []


async def test_rag_pipeline_run_retention_uses_notebook_policy_defaults(tmp_path: Path) -> None:
    from zen_claw.knowledge.pipeline import RAGPipeline

    doc_a = tmp_path / "a.txt"
    doc_b = tmp_path / "b.txt"
    doc_a.write_text("Alpha policy", encoding="utf-8")
    doc_b.write_text("Beta policy", encoding="utf-8")

    pipeline = RAGPipeline(tmp_path, store_kind="memory")
    first = await pipeline.ingest(str(doc_a), notebook_id="kb")
    second = await pipeline.ingest(str(doc_b), notebook_id="kb")
    pipeline.ensure_notebook("kb", store_backend="memory")
    pipeline._manager.set_retention_policy("kb", retention_max_documents=1, retention_max_age_days=0)  # type: ignore[attr-defined]

    retained = pipeline.run_retention(notebook_id="kb")
    remaining = pipeline.list_documents(notebook_id="kb")

    assert retained["max_documents"] == 1
    assert retained["documents_removed"] == 1
    assert first["document_id"] in retained["document_ids"]
    assert second["document_id"] not in retained["document_ids"]
    assert remaining["total_documents"] == 1


async def test_rag_pipeline_run_retention_prefers_first_ingested_when_timestamps_collide(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from datetime import UTC, datetime

    from zen_claw.knowledge.pipeline import RAGPipeline

    doc_a = tmp_path / "a.txt"
    doc_b = tmp_path / "b.txt"
    doc_a.write_text("Alpha policy", encoding="utf-8")
    doc_b.write_text("Beta policy", encoding="utf-8")

    fixed_now = datetime(2026, 3, 7, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr("zen_claw.knowledge.notebook.datetime", type("_FixedDateTime", (), {
        "now": staticmethod(lambda _tz=None: fixed_now),
        "fromisoformat": staticmethod(datetime.fromisoformat),
        "min": datetime.min,
    }))

    pipeline = RAGPipeline(tmp_path, store_kind="memory")
    first = await pipeline.ingest(str(doc_a), notebook_id="kb")
    second = await pipeline.ingest(str(doc_b), notebook_id="kb")

    retained = pipeline.run_retention(notebook_id="kb", max_documents=1, max_age_days=0)

    assert retained["documents_removed"] == 1
    assert first["document_id"] in retained["document_ids"]
    assert second["document_id"] not in retained["document_ids"]

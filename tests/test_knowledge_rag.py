"""Tests for RAG foundation."""

from __future__ import annotations

import json
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
    rows = chunker.chunk_with_metadata("Some content.\n\nMore content.", source="x.txt", page=1)
    assert isinstance(rows, list)
    for row in rows:
        assert row["source"] == "x.txt"
        assert row["page"] == 1
        assert "chunk_index" in row


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
    from zen_claw.knowledge.notebook import NotebookManager
    from zen_claw.knowledge.retriever import HybridSearchResult

    manager = NotebookManager(tmp_path)
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

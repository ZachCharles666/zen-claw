"""Tests for B4 — Knowledge document content preview."""

from __future__ import annotations

from pathlib import Path

import pytest

from zen_claw.knowledge.notebook import NotebookManager
from zen_claw.knowledge.pipeline import RAGPipeline


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_pipeline(tmp_path: Path) -> RAGPipeline:
    return RAGPipeline(tmp_path, store_kind="memory")


def _seed_doc(pipeline: RAGPipeline, source_file: Path, notebook_id: str = "default") -> str:
    """Register *source_file* in the notebook and return its document_id."""
    nb_mgr = NotebookManager(pipeline.data_dir)
    nb = nb_mgr.get_or_create(notebook_id, store_backend="memory")
    doc_id = nb_mgr.record_source(nb.id, str(source_file), doc_units=1)
    return doc_id


# ── preview_document ─────────────────────────────────────────────────────────

def test_preview_returns_content(tmp_path: Path):
    src = tmp_path / "doc.txt"
    src.write_text("Hello, world! This is the document content.", encoding="utf-8")
    pipeline = _make_pipeline(tmp_path)
    doc_id = _seed_doc(pipeline, src)

    result = pipeline.preview_document(doc_id)
    assert result["available"] is True
    assert result["content"] == "Hello, world! This is the document content."
    assert result["truncated"] is False


def test_preview_truncates_long_content(tmp_path: Path):
    src = tmp_path / "big.txt"
    src.write_text("A" * 5000, encoding="utf-8")
    pipeline = _make_pipeline(tmp_path)
    doc_id = _seed_doc(pipeline, src)

    result = pipeline.preview_document(doc_id, max_chars=100)
    assert result["available"] is True
    assert result["truncated"] is True
    assert len(result["content"]) == 100


def test_preview_missing_source_file(tmp_path: Path):
    """If source file was deleted, available=False and content is empty."""
    src = tmp_path / "gone.txt"
    src.write_text("original", encoding="utf-8")
    pipeline = _make_pipeline(tmp_path)
    doc_id = _seed_doc(pipeline, src)
    src.unlink()  # delete the file

    result = pipeline.preview_document(doc_id)
    assert result["available"] is False
    assert result["content"] == ""


def test_preview_document_not_found_raises(tmp_path: Path):
    pipeline = _make_pipeline(tmp_path)
    # Need a notebook to exist
    NotebookManager(pipeline.data_dir).get_or_create("default", store_backend="memory")

    with pytest.raises(ValueError, match="document not found"):
        pipeline.preview_document("nonexistent-doc-id")


def test_preview_notebook_not_found_raises(tmp_path: Path):
    pipeline = _make_pipeline(tmp_path)
    with pytest.raises(ValueError, match="notebook not found"):
        pipeline.preview_document("any-id", notebook_id="does-not-exist")


# ── search_documents ──────────────────────────────────────────────────────────

def test_search_returns_matching_documents(tmp_path: Path):
    pipeline = _make_pipeline(tmp_path)
    nb_mgr = NotebookManager(pipeline.data_dir)
    nb = nb_mgr.get_or_create("default", store_backend="memory")
    nb_mgr.record_source(nb.id, "/data/reports/annual_report.pdf", doc_units=1)
    nb_mgr.record_source(nb.id, "/data/docs/manual.txt", doc_units=1)

    result = pipeline.search_documents("annual")
    assert result["total_documents"] == 1
    assert "annual" in result["documents"][0]["source"]


def test_search_empty_query_returns_all(tmp_path: Path):
    pipeline = _make_pipeline(tmp_path)
    nb_mgr = NotebookManager(pipeline.data_dir)
    nb = nb_mgr.get_or_create("default", store_backend="memory")
    nb_mgr.record_source(nb.id, "/data/a.txt", doc_units=1)
    nb_mgr.record_source(nb.id, "/data/b.txt", doc_units=1)

    result = pipeline.search_documents("")
    assert result["total_documents"] == 2


def test_search_no_match_returns_empty(tmp_path: Path):
    pipeline = _make_pipeline(tmp_path)
    nb_mgr = NotebookManager(pipeline.data_dir)
    nb = nb_mgr.get_or_create("default", store_backend="memory")
    nb_mgr.record_source(nb.id, "/data/readme.txt", doc_units=1)

    result = pipeline.search_documents("zzznomatch")
    assert result["total_documents"] == 0
    assert result["documents"] == []

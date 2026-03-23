"""Tests for RAGPipeline.list_notebooks / create_notebook / repair_notebook
and the corresponding API endpoints.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

try:
    from fastapi.testclient import TestClient as _TC
    from zen_claw.dashboard.server import api_app, generate_api_key, store_api_key
    import zen_claw.dashboard.server as _srv_mod

    _FASTAPI_AVAILABLE = api_app is not None
except Exception:
    _FASTAPI_AVAILABLE = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _skip_no_fastapi():
    if not _FASTAPI_AVAILABLE:
        pytest.skip("fastapi not available")


def _pipeline(data_dir: Path, tenant_id: str = "default"):
    from zen_claw.knowledge.pipeline import RAGPipeline
    return RAGPipeline(data_dir, tenant_id=tenant_id)


def _fake_config(data_dir: Path):
    from zen_claw.config.schema import Config
    return Config.model_validate({"agents": {"defaults": {"workspace": str(data_dir / "ws")}}})


def _api_setup(tmp_path: Path, monkeypatch):
    _skip_no_fastapi()
    import zen_claw.config.loader as loader_mod

    monkeypatch.setattr(loader_mod, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(loader_mod, "load_config", lambda: _fake_config(tmp_path))
    monkeypatch.setattr(_srv_mod, "_api_keys_file", lambda: tmp_path / "api_keys.json")
    raw, _ = generate_api_key()
    store_api_key(raw)
    return raw


# ---------------------------------------------------------------------------
# RAGPipeline.list_notebooks
# ---------------------------------------------------------------------------


class TestListNotebooks:
    def test_list_notebooks_empty(self, tmp_path: Path) -> None:
        """Empty tenant → notebooks=[]."""
        pipeline = _pipeline(tmp_path)
        result = pipeline.list_notebooks()
        assert result == []

    def test_list_notebooks_after_ingest(self, tmp_path: Path) -> None:
        """After ensure_notebook, list_notebooks returns that notebook."""
        pipeline = _pipeline(tmp_path)
        pipeline.ensure_notebook("mynotebook")
        result = pipeline.list_notebooks()
        ids = [r["notebook_id"] for r in result]
        assert "mynotebook" in ids

    def test_list_notebooks_backend_ok_field(self, tmp_path: Path) -> None:
        """Each returned notebook has backend_ok (bool) field."""
        pipeline = _pipeline(tmp_path)
        pipeline.ensure_notebook("nb1")
        result = pipeline.list_notebooks()
        assert len(result) >= 1
        for row in result:
            assert "backend_ok" in row
            assert isinstance(row["backend_ok"], bool)


# ---------------------------------------------------------------------------
# RAGPipeline.create_notebook
# ---------------------------------------------------------------------------


class TestCreateNotebook:
    def test_create_notebook_ok(self, tmp_path: Path) -> None:
        """create_notebook returns ok=True and created=True."""
        pipeline = _pipeline(tmp_path)
        result = pipeline.create_notebook("test_kb")
        assert result["ok"] is True
        assert result["created"] is True
        assert result["notebook_id"] == "test_kb"

    def test_create_notebook_duplicate(self, tmp_path: Path) -> None:
        """Duplicate notebook_id raises ValueError."""
        pipeline = _pipeline(tmp_path)
        pipeline.create_notebook("dup_kb")
        with pytest.raises(ValueError, match="already exists"):
            pipeline.create_notebook("dup_kb")

    def test_create_notebook_sets_display_name(self, tmp_path: Path) -> None:
        """display_name takes precedence over notebook_id for the name field."""
        pipeline = _pipeline(tmp_path)
        result = pipeline.create_notebook("finance_id", display_name="Finance KB")
        assert result["display_name"] == "Finance KB"


# ---------------------------------------------------------------------------
# RAGPipeline.repair_notebook
# ---------------------------------------------------------------------------


class TestRepairNotebook:
    def test_repair_notebook_healthy(self, tmp_path: Path) -> None:
        """Healthy backend → actions_taken contains 'no_repair_needed'."""
        pipeline = _pipeline(tmp_path, tenant_id="default")
        pipeline.ensure_notebook("healthy_nb")
        result = pipeline.repair_notebook("healthy_nb")
        assert "no_repair_needed" in result["actions_taken"]

    def test_repair_notebook_empty_backend_init_failure_is_still_no_repair_needed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Empty notebook should not report repair work when backend init fails."""
        from zen_claw.knowledge import pipeline as pipeline_mod

        pipeline = _pipeline(tmp_path, tenant_id="default")
        pipeline.ensure_notebook("healthy_nb")

        def _raise_backend_init(*_args, **_kwargs):
            raise RuntimeError("backend unavailable")

        monkeypatch.setattr(pipeline_mod.HybridRetriever, "from_notebook", _raise_backend_init)

        result = pipeline.repair_notebook("healthy_nb")

        assert result["ok"] is True
        assert "no_repair_needed" in result["actions_taken"]

    def test_repair_notebook_memory_unsupported(self, tmp_path: Path) -> None:
        """Memory backend → actions indicate ephemeral, ok=True."""
        pipeline = _pipeline(tmp_path, tenant_id="default")
        pipeline.ensure_notebook("mem_nb", store_backend="memory")
        result = pipeline.repair_notebook("mem_nb")
        # Memory backend should either report no_repair_needed or ephemeral
        assert result["notebook_id"] == "mem_nb"
        joined = " ".join(result["actions_taken"] + result["errors"])
        assert "no_repair_needed" in joined or "ephemeral" in joined

    def test_repair_notebook_not_found(self, tmp_path: Path) -> None:
        """Nonexistent notebook → raises ValueError."""
        pipeline = _pipeline(tmp_path)
        with pytest.raises(ValueError, match="not found"):
            pipeline.repair_notebook("ghost_notebook")


# ---------------------------------------------------------------------------
# Activity history notebook_id filter
# ---------------------------------------------------------------------------


class TestActivityHistoryNotebookFilter:
    def test_activity_history_notebook_filter(self, tmp_path: Path) -> None:
        """_read_knowledge_activity_history filters by notebook_id via target_id."""
        from zen_claw.dashboard.server import _read_knowledge_activity_history

        log_dir = tmp_path / "dashboard"
        log_dir.mkdir(parents=True, exist_ok=True)
        # Write two cron log entries with different notebook_ids
        (log_dir / "knowledge_cron.log.jsonl").write_text(
            json.dumps({"at_ms": 1000, "knowledge_notebook": "nb_a", "status": "ok"}) + "\n"
            + json.dumps({"at_ms": 2000, "knowledge_notebook": "nb_b", "status": "ok"}) + "\n"
        )
        (log_dir / "knowledge_policy.log.jsonl").write_text("")

        result = _read_knowledge_activity_history(data_dir=tmp_path, notebook_id="nb_a")
        assert result["total"] == 1
        assert result["rows"][0]["target_id"] == "nb_a"

    def test_activity_history_csv_export(self, tmp_path: Path, monkeypatch) -> None:
        """GET /api/v1/rag/activity-history?format=csv → Content-Type text/csv."""
        raw = _api_setup(tmp_path, monkeypatch)
        log_dir = tmp_path / "dashboard"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "knowledge_cron.log.jsonl").write_text(
            json.dumps({"at_ms": 1000, "knowledge_notebook": "nb_a", "status": "ok"}) + "\n"
        )
        (log_dir / "knowledge_policy.log.jsonl").write_text("")

        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.get(
            "/api/v1/rag/activity-history",
            params={"format": "csv"},
            headers={"X-API-Key": raw},
        )
        assert resp.status_code == 200
        assert "text/csv" in resp.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestNotebookAPI:
    def test_list_notebooks_api_returns_200(self, tmp_path: Path, monkeypatch) -> None:
        """GET /api/v1/rag/notebooks → 200 with notebooks/total fields."""
        raw = _api_setup(tmp_path, monkeypatch)
        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.get("/api/v1/rag/notebooks", headers={"X-API-Key": raw})
        assert resp.status_code == 200
        data = resp.json()
        assert "notebooks" in data
        assert "total" in data

    def test_create_notebook_api_returns_200(self, tmp_path: Path, monkeypatch) -> None:
        """POST /api/v1/rag/notebooks → 200, ok=True."""
        raw = _api_setup(tmp_path, monkeypatch)
        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/rag/notebooks",
            json={"notebook_id": "api_test_kb"},
            headers={"X-API-Key": raw},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("ok") is True

    def test_create_notebook_api_duplicate_409(self, tmp_path: Path, monkeypatch) -> None:
        """Duplicate POST /api/v1/rag/notebooks → 409."""
        raw = _api_setup(tmp_path, monkeypatch)
        client = _TC(api_app, raise_server_exceptions=False)
        client.post(
            "/api/v1/rag/notebooks",
            json={"notebook_id": "dup_api_kb"},
            headers={"X-API-Key": raw},
        )
        resp = client.post(
            "/api/v1/rag/notebooks",
            json={"notebook_id": "dup_api_kb"},
            headers={"X-API-Key": raw},
        )
        assert resp.status_code == 409

    def test_repair_notebook_api_returns_200(self, tmp_path: Path, monkeypatch) -> None:
        """POST /api/v1/rag/notebook/default/repair → 200 with actions_taken."""
        raw = _api_setup(tmp_path, monkeypatch)
        # Create a notebook first
        pipeline = _pipeline(tmp_path)
        pipeline.ensure_notebook("default")

        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/rag/notebook/default/repair",
            headers={"X-API-Key": raw},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "actions_taken" in data

    def test_activity_history_api_notebook_filter(self, tmp_path: Path, monkeypatch) -> None:
        """GET /api/v1/rag/activity-history?notebook_id=x → 200, filtered rows."""
        raw = _api_setup(tmp_path, monkeypatch)
        log_dir = tmp_path / "dashboard"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "knowledge_cron.log.jsonl").write_text(
            json.dumps({"at_ms": 1000, "knowledge_notebook": "special_nb", "status": "ok"}) + "\n"
            + json.dumps({"at_ms": 2000, "knowledge_notebook": "other_nb", "status": "ok"}) + "\n"
        )
        (log_dir / "knowledge_policy.log.jsonl").write_text("")

        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.get(
            "/api/v1/rag/activity-history",
            params={"notebook_id": "special_nb", "limit": 10},
            headers={"X-API-Key": raw},
        )
        assert resp.status_code == 200
        data = resp.json()
        for row in data.get("rows", []):
            assert row["target_id"] == "special_nb"

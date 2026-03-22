"""Tests for Crawler Schedule lifecycle API (Phase 3.5.2)."""

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


def _make_sources_file(path: Path, names: list[str]) -> Path:
    sources_dir = path / "crawler"
    sources_dir.mkdir(parents=True, exist_ok=True)
    sources_path = sources_dir / "sources.json"
    payload = {
        "sources": [
            {
                "name": name,
                "url": f"https://example.com/{name}",
                "notebook": "crawler_kb",
                "selector": "",
                "use_browser": False,
                "max_chars": 5000,
                "metadata": {},
                "enabled": True,
            }
            for name in names
        ]
    }
    sources_path.write_text(json.dumps(payload), encoding="utf-8")
    return sources_path


def _schedules_path(tmp_path: Path) -> Path:
    return tmp_path / "crawler" / "schedules.json"


def _skip_no_fastapi():
    if not _FASTAPI_AVAILABLE:
        pytest.skip("fastapi not available")


def _fake_config(workspace: Path):
    from zen_claw.config.schema import Config

    return Config.model_validate({"agents": {"defaults": {"workspace": str(workspace)}}})


def _setup_api(tmp_path: Path, monkeypatch, source_names: list[str] | None = None):
    _skip_no_fastapi()
    import zen_claw.config.loader as loader_mod

    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    _make_sources_file(tmp_path, source_names or ["news", "finance"])
    monkeypatch.setattr(loader_mod, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(loader_mod, "load_config", lambda: _fake_config(ws))
    monkeypatch.setattr(_srv_mod, "_api_keys_file", lambda: tmp_path / "api_keys.json")
    raw, _ = generate_api_key()
    store_api_key(raw)
    return raw


# ---------------------------------------------------------------------------
# Unit tests — scheduler utility functions
# ---------------------------------------------------------------------------


class TestUpsertSchedule:
    def test_upsert_schedule_creates_entry(self, tmp_path: Path) -> None:
        """upsert_schedule creates a new entry in schedules.json."""
        from zen_claw.skills.crawler.scheduler import upsert_schedule, load_schedules_json

        path = _schedules_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        upsert_schedule(path, {"source_name": "news", "cron": "0 */6 * * *", "enabled": True})
        rows = load_schedules_json(path)
        assert len(rows) == 1
        assert rows[0]["source_name"] == "news"

    def test_upsert_schedule_overwrites(self, tmp_path: Path) -> None:
        """Second upsert with same source_name replaces the first entry."""
        from zen_claw.skills.crawler.scheduler import upsert_schedule, load_schedules_json

        path = _schedules_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        upsert_schedule(path, {"source_name": "news", "cron": "0 */6 * * *"})
        upsert_schedule(path, {"source_name": "news", "cron": "0 8 * * *"})
        rows = load_schedules_json(path)
        assert len(rows) == 1
        assert rows[0]["cron"] == "0 8 * * *"


class TestDeleteSchedule:
    def test_delete_schedule_found(self, tmp_path: Path) -> None:
        """delete_schedule removes entry, returns True."""
        from zen_claw.skills.crawler.scheduler import upsert_schedule, delete_schedule, load_schedules_json

        path = _schedules_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        upsert_schedule(path, {"source_name": "news", "cron": "0 */6 * * *"})
        result = delete_schedule(path, "news")
        assert result is True
        assert load_schedules_json(path) == []

    def test_delete_schedule_not_found(self, tmp_path: Path) -> None:
        """delete_schedule returns False when source_name not present."""
        from zen_claw.skills.crawler.scheduler import delete_schedule

        path = _schedules_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        result = delete_schedule(path, "ghost")
        assert result is False


class TestSetScheduleEnabled:
    def test_set_schedule_enabled_pause(self, tmp_path: Path) -> None:
        """set_schedule_enabled(False) pauses the schedule."""
        from zen_claw.skills.crawler.scheduler import upsert_schedule, set_schedule_enabled, load_schedules_json

        path = _schedules_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        upsert_schedule(path, {"source_name": "news", "cron": "0 */6 * * *", "enabled": True})
        result = set_schedule_enabled(path, "news", False)
        assert result is True
        rows = load_schedules_json(path)
        assert rows[0]["enabled"] is False

    def test_set_schedule_enabled_resume(self, tmp_path: Path) -> None:
        """set_schedule_enabled(True) resumes a paused schedule."""
        from zen_claw.skills.crawler.scheduler import upsert_schedule, set_schedule_enabled, load_schedules_json

        path = _schedules_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        upsert_schedule(path, {"source_name": "news", "cron": "0 */6 * * *", "enabled": False})
        result = set_schedule_enabled(path, "news", True)
        assert result is True
        rows = load_schedules_json(path)
        assert rows[0]["enabled"] is True


# ---------------------------------------------------------------------------
# API tests
# ---------------------------------------------------------------------------


class TestSchedulesListAPI:
    def test_schedules_list_api_returns_200(self, tmp_path: Path, monkeypatch) -> None:
        """GET /api/v1/crawler/schedules → 200, contains schedules/total."""
        raw = _setup_api(tmp_path, monkeypatch)
        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.get("/api/v1/crawler/schedules", headers={"X-API-Key": raw})
        assert resp.status_code == 200
        data = resp.json()
        assert "schedules" in data
        assert "total" in data


class TestCreateScheduleAPI:
    def test_create_schedule_api_returns_200(self, tmp_path: Path, monkeypatch) -> None:
        """POST /api/v1/crawler/schedules → 200, ok=True."""
        raw = _setup_api(tmp_path, monkeypatch, ["news"])
        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/crawler/schedules",
            json={"source_name": "news", "cron": "0 */6 * * *"},
            headers={"X-API-Key": raw},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_create_schedule_api_source_not_found_404(self, tmp_path: Path, monkeypatch) -> None:
        """POST schedule for non-existent source → 404."""
        raw = _setup_api(tmp_path, monkeypatch, ["news"])
        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/crawler/schedules",
            json={"source_name": "ghost_xyz", "cron": "0 */6 * * *"},
            headers={"X-API-Key": raw},
        )
        assert resp.status_code == 404

    def test_schedule_audit_written(self, tmp_path: Path, monkeypatch) -> None:
        """POST schedule writes event='crawler.schedule.upsert' to audit log."""
        raw = _setup_api(tmp_path, monkeypatch, ["news"])
        client = _TC(api_app, raise_server_exceptions=False)
        client.post(
            "/api/v1/crawler/schedules",
            json={"source_name": "news", "cron": "0 */6 * * *"},
            headers={"X-API-Key": raw},
        )
        log_path = tmp_path / "dashboard" / "crawler_sources.log.jsonl"
        assert log_path.exists()
        events = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        upsert_events = [e for e in events if e.get("event") == "crawler.schedule.upsert"]
        assert len(upsert_events) >= 1


class TestDeleteScheduleAPI:
    def test_delete_schedule_api_returns_200(self, tmp_path: Path, monkeypatch) -> None:
        """DELETE /api/v1/crawler/schedules/{name} → 200."""
        raw = _setup_api(tmp_path, monkeypatch, ["news"])
        client = _TC(api_app, raise_server_exceptions=False)
        # Create first
        client.post(
            "/api/v1/crawler/schedules",
            json={"source_name": "news", "cron": "0 */6 * * *"},
            headers={"X-API-Key": raw},
        )
        resp = client.delete("/api/v1/crawler/schedules/news", headers={"X-API-Key": raw})
        assert resp.status_code == 200

    def test_delete_schedule_api_404(self, tmp_path: Path, monkeypatch) -> None:
        """DELETE non-existent schedule → 404."""
        raw = _setup_api(tmp_path, monkeypatch, ["news"])
        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.delete("/api/v1/crawler/schedules/ghost_xyz", headers={"X-API-Key": raw})
        assert resp.status_code == 404


class TestPauseResumeAPI:
    def _create_schedule(self, client, raw):
        client.post(
            "/api/v1/crawler/schedules",
            json={"source_name": "news", "cron": "0 */6 * * *"},
            headers={"X-API-Key": raw},
        )

    def test_pause_schedule_api_returns_200(self, tmp_path: Path, monkeypatch) -> None:
        """POST .../pause → 200, enabled=False."""
        raw = _setup_api(tmp_path, monkeypatch, ["news"])
        client = _TC(api_app, raise_server_exceptions=False)
        self._create_schedule(client, raw)
        resp = client.post("/api/v1/crawler/schedules/news/pause", headers={"X-API-Key": raw})
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

    def test_resume_schedule_api_returns_200(self, tmp_path: Path, monkeypatch) -> None:
        """POST .../resume → 200, enabled=True."""
        raw = _setup_api(tmp_path, monkeypatch, ["news"])
        client = _TC(api_app, raise_server_exceptions=False)
        self._create_schedule(client, raw)
        client.post("/api/v1/crawler/schedules/news/pause", headers={"X-API-Key": raw})
        resp = client.post("/api/v1/crawler/schedules/news/resume", headers={"X-API-Key": raw})
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True

    def test_pause_missing_schedule_404(self, tmp_path: Path, monkeypatch) -> None:
        """Pause non-existent schedule → 404."""
        raw = _setup_api(tmp_path, monkeypatch, ["news"])
        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.post("/api/v1/crawler/schedules/ghost_xyz/pause", headers={"X-API-Key": raw})
        assert resp.status_code == 404

"""Tests for Crawler Source lifecycle: delete/disable/enable/status endpoints."""

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
    """Write a sources.json with stub sources."""
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


def _load_sources(path: Path) -> list[dict]:
    from zen_claw.skills.crawler.scheduler import load_sources_json

    return [
        {
            "name": s.name,
            "enabled": s.enabled,
        }
        for s in load_sources_json(path)
    ]


def _skip_no_fastapi():
    if not _FASTAPI_AVAILABLE:
        pytest.skip("fastapi not available")


def _fake_config(workspace: Path):
    from zen_claw.config.schema import Config

    return Config.model_validate({"agents": {"defaults": {"workspace": str(workspace)}}})


def _setup_api(tmp_path: Path, monkeypatch, source_names: list[str]):
    _skip_no_fastapi()
    import zen_claw.config.loader as loader_mod

    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    sources_path = _make_sources_file(tmp_path, source_names)
    monkeypatch.setattr(loader_mod, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(loader_mod, "load_config", lambda: _fake_config(ws))
    monkeypatch.setattr(_srv_mod, "_api_keys_file", lambda: tmp_path / "api_keys.json")
    raw, _ = generate_api_key()
    store_api_key(raw)
    return sources_path, raw


# ---------------------------------------------------------------------------
# Unit tests — scheduler utility functions
# ---------------------------------------------------------------------------


class TestDeleteSource:
    def test_delete_source_removes_entry(self, tmp_path: Path) -> None:
        """delete_source removes the matching entry from sources.json."""
        from zen_claw.skills.crawler.scheduler import delete_source, load_sources_json

        path = _make_sources_file(tmp_path, ["alpha", "beta"])
        result = delete_source(path, "alpha")
        assert result is True
        names = [s.name for s in load_sources_json(path)]
        assert "alpha" not in names
        assert "beta" in names

    def test_delete_source_nonexistent_returns_false(self, tmp_path: Path) -> None:
        """Deleting a non-existent source returns False."""
        from zen_claw.skills.crawler.scheduler import delete_source

        path = _make_sources_file(tmp_path, ["alpha"])
        result = delete_source(path, "nonexistent_xyz")
        assert result is False

    def test_delete_source_case_insensitive(self, tmp_path: Path) -> None:
        """delete_source is case-insensitive for name matching."""
        from zen_claw.skills.crawler.scheduler import delete_source, load_sources_json

        path = _make_sources_file(tmp_path, ["MySource"])
        result = delete_source(path, "mysource")
        assert result is True
        assert load_sources_json(path) == []


class TestSetSourceEnabled:
    def test_set_source_enabled_false(self, tmp_path: Path) -> None:
        """set_source_enabled(..., False) marks source as disabled."""
        from zen_claw.skills.crawler.scheduler import set_source_enabled, load_sources_json

        path = _make_sources_file(tmp_path, ["news"])
        result = set_source_enabled(path, "news", False)
        assert result is True
        sources = load_sources_json(path)
        assert sources[0].enabled is False

    def test_set_source_enabled_true(self, tmp_path: Path) -> None:
        """set_source_enabled(..., True) re-enables a disabled source."""
        from zen_claw.skills.crawler.scheduler import set_source_enabled, load_sources_json

        path = _make_sources_file(tmp_path, ["news"])
        # first disable
        set_source_enabled(path, "news", False)
        # then re-enable
        result = set_source_enabled(path, "news", True)
        assert result is True
        sources = load_sources_json(path)
        assert sources[0].enabled is True

    def test_set_source_enabled_nonexistent(self, tmp_path: Path) -> None:
        """set_source_enabled on non-existent source returns False."""
        from zen_claw.skills.crawler.scheduler import set_source_enabled

        path = _make_sources_file(tmp_path, ["alpha"])
        result = set_source_enabled(path, "ghost", False)
        assert result is False


# ---------------------------------------------------------------------------
# API tests
# ---------------------------------------------------------------------------


class TestDeleteAPI:
    def test_delete_api_returns_200(self, tmp_path: Path, monkeypatch) -> None:
        """DELETE /api/v1/crawler/sources/{name} → 200, ok=True."""
        sources_path, raw = _setup_api(tmp_path, monkeypatch, ["news"])
        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.delete("/api/v1/crawler/sources/news", headers={"X-API-Key": raw})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json()["deleted"] == "news"

    def test_delete_api_404_not_found(self, tmp_path: Path, monkeypatch) -> None:
        """DELETE non-existent source → 404."""
        sources_path, raw = _setup_api(tmp_path, monkeypatch, ["news"])
        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.delete("/api/v1/crawler/sources/ghost_xyz", headers={"X-API-Key": raw})
        assert resp.status_code == 404

    def test_delete_api_writes_audit(self, tmp_path: Path, monkeypatch) -> None:
        """DELETE writes event='crawler.source.delete' to audit log."""
        sources_path, raw = _setup_api(tmp_path, monkeypatch, ["news"])
        client = _TC(api_app, raise_server_exceptions=False)
        client.delete("/api/v1/crawler/sources/news", headers={"X-API-Key": raw})
        log_path = tmp_path / "dashboard" / "crawler_sources.log.jsonl"
        assert log_path.exists()
        events = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        delete_events = [e for e in events if e.get("event") == "crawler.source.delete"]
        assert len(delete_events) >= 1
        assert delete_events[0]["source_name"] == "news"


class TestDisableEnableAPI:
    def test_disable_api_returns_200(self, tmp_path: Path, monkeypatch) -> None:
        """POST .../disable → 200, enabled=False."""
        sources_path, raw = _setup_api(tmp_path, monkeypatch, ["news"])
        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.post("/api/v1/crawler/sources/news/disable", headers={"X-API-Key": raw})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["enabled"] is False

    def test_enable_api_returns_200(self, tmp_path: Path, monkeypatch) -> None:
        """POST .../enable → 200, enabled=True."""
        sources_path, raw = _setup_api(tmp_path, monkeypatch, ["news"])
        client = _TC(api_app, raise_server_exceptions=False)
        # disable first
        client.post("/api/v1/crawler/sources/news/disable", headers={"X-API-Key": raw})
        # then enable
        resp = client.post("/api/v1/crawler/sources/news/enable", headers={"X-API-Key": raw})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["enabled"] is True

    def test_disable_enable_404(self, tmp_path: Path, monkeypatch) -> None:
        """Disable non-existent source → 404."""
        sources_path, raw = _setup_api(tmp_path, monkeypatch, ["news"])
        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.post("/api/v1/crawler/sources/ghost_xyz/disable", headers={"X-API-Key": raw})
        assert resp.status_code == 404

    def test_disable_persists_across_list(self, tmp_path: Path, monkeypatch) -> None:
        """After disable, GET /sources shows enabled=False for that source."""
        sources_path, raw = _setup_api(tmp_path, monkeypatch, ["news", "finance"])
        client = _TC(api_app, raise_server_exceptions=False)
        client.post("/api/v1/crawler/sources/news/disable", headers={"X-API-Key": raw})
        resp = client.get("/api/v1/crawler/sources", headers={"X-API-Key": raw})
        assert resp.status_code == 200
        sources_by_name = {s["name"]: s for s in resp.json()["sources"]}
        assert sources_by_name["news"]["enabled"] is False
        # finance should still be enabled
        assert sources_by_name["finance"]["enabled"] is True


class TestStatusAPI:
    def test_status_api_returns_200(self, tmp_path: Path, monkeypatch) -> None:
        """GET .../status → 200, contains source_name and enabled."""
        sources_path, raw = _setup_api(tmp_path, monkeypatch, ["news"])
        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.get("/api/v1/crawler/sources/news/status", headers={"X-API-Key": raw})
        assert resp.status_code == 200
        data = resp.json()
        assert data["source_name"] == "news"
        assert "enabled" in data

    def test_status_api_404(self, tmp_path: Path, monkeypatch) -> None:
        """Status of non-existent source → 404."""
        sources_path, raw = _setup_api(tmp_path, monkeypatch, ["news"])
        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.get("/api/v1/crawler/sources/ghost_xyz/status", headers={"X-API-Key": raw})
        assert resp.status_code == 404

    def test_status_api_fields(self, tmp_path: Path, monkeypatch) -> None:
        """Status response contains run_count and recent_runs fields."""
        sources_path, raw = _setup_api(tmp_path, monkeypatch, ["news"])
        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.get("/api/v1/crawler/sources/news/status", headers={"X-API-Key": raw})
        data = resp.json()
        assert "run_count" in data
        assert "recent_runs" in data
        assert isinstance(data["recent_runs"], list)

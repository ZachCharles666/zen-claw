"""Tests for _build_skill_health_summary and POST /api/v1/skills/bulk-action."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

try:
    from fastapi.testclient import TestClient as _TestClient

    import zen_claw.dashboard.server as _srv_mod
    from zen_claw.dashboard.server import api_app, generate_api_key, store_api_key

    _FASTAPI_AVAILABLE = api_app is not None
except Exception:
    _FASTAPI_AVAILABLE = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_skill_dir(workspace: Path, name: str, *, enabled: bool = True, trust: str = "trusted") -> Path:
    skill_dir = workspace / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": name,
        "version": "1.0.0",
        "description": f"Test skill {name}",
        "trust": trust,
        "runtime_contract": {"intent": name, "intent_mode": "tool"},
    }
    (skill_dir / "manifest.json").write_text(json.dumps(manifest))
    (skill_dir / "SKILL.md").write_text(f"# {name}")
    if not enabled:
        disabled_dir = workspace / ".zen-claw" / "skills-state"
        disabled_dir.mkdir(parents=True, exist_ok=True)
        disabled_file = disabled_dir / "disabled.json"
        current = json.loads(disabled_file.read_text()) if disabled_file.exists() else []
        if name not in current:
            current.append(name)
        disabled_file.write_text(json.dumps(current))
    return skill_dir


def _make_loader(workspace: Path):
    from zen_claw.agent.skills import SkillsLoader
    return SkillsLoader(workspace)


def _make_inventory_rows(workspace: Path) -> list[dict]:
    loader = _make_loader(workspace)
    inventory = loader.build_skills_inventory()
    return list(inventory.get("skills") or [])


def _skip_no_fastapi():
    if not _FASTAPI_AVAILABLE:
        pytest.skip("fastapi not available")


def _fake_config(workspace: Path):
    from zen_claw.config.schema import Config
    return Config.model_validate({"agents": {"defaults": {"workspace": str(workspace)}}})


# ---------------------------------------------------------------------------
# _build_skill_health_summary unit tests
# ---------------------------------------------------------------------------


class TestHealthSummaryUnit:
    def test_health_summary_empty_workspace(self, tmp_path: Path) -> None:
        """Empty workspace → total=0, all counts 0, pcts 0.0."""
        from zen_claw.dashboard.server import _build_skill_health_summary

        result = _build_skill_health_summary([])
        assert result["total"] == 0
        assert result["enabled"] == 0
        assert result["manifest_ok"] == 0
        assert result["trusted"] == 0
        assert result["runtime_covered"] == 0
        assert result["preflight_ok"] == 0
        assert result["preflight_issues"] == 0
        assert result["enabled_pct"] == 0.0
        assert result["trusted_pct"] == 0.0
        assert result["preflight_ok_pct"] == 0.0

    def test_health_summary_counts_enabled(self, tmp_path: Path) -> None:
        """2 rows, 1 enabled → enabled=1, enabled_pct=50.0."""
        from zen_claw.dashboard.server import _build_skill_health_summary

        rows = [
            {"name": "alpha", "enabled": True,  "manifest": "valid", "trust": "trusted", "runtime_intent_mode": "tool", "tests_present": True},
            {"name": "beta",  "enabled": False, "manifest": "valid", "trust": "",        "runtime_intent_mode": "",     "tests_present": False},
        ]
        result = _build_skill_health_summary(rows)
        assert result["total"] == 2
        assert result["enabled"] == 1
        assert result["enabled_pct"] == 50.0

    def test_health_summary_counts_manifest_ok(self, tmp_path: Path) -> None:
        """manifest='valid' rows → manifest_ok counted correctly."""
        from zen_claw.dashboard.server import _build_skill_health_summary

        rows = [
            {"name": "a", "enabled": True, "manifest": "valid",   "trust": "", "runtime_intent_mode": "", "tests_present": False},
            {"name": "b", "enabled": True, "manifest": "invalid", "trust": "", "runtime_intent_mode": "", "tests_present": False},
        ]
        result = _build_skill_health_summary(rows)
        assert result["manifest_ok"] == 1

    def test_health_summary_pct_fields_present(self, tmp_path: Path) -> None:
        """Result always contains enabled_pct / trusted_pct / preflight_ok_pct."""
        from zen_claw.dashboard.server import _build_skill_health_summary

        rows = [
            {"name": "x", "enabled": True, "manifest": "valid", "trust": "trusted", "runtime_intent_mode": "tool", "tests_present": True},
        ]
        result = _build_skill_health_summary(rows)
        assert "enabled_pct" in result
        assert "trusted_pct" in result
        assert "preflight_ok_pct" in result

    def test_health_summary_with_preflight_checks(self, tmp_path: Path) -> None:
        """Passing latest_checks populates preflight_ok/issues."""
        from zen_claw.dashboard.server import _build_skill_health_summary

        rows = [{"name": "a", "enabled": True, "manifest": "valid", "trust": "trusted", "runtime_intent_mode": "tool", "tests_present": False}]
        checks = {
            "a": {"ok": True},
            "b": {"ok": False},
        }
        result = _build_skill_health_summary(rows, latest_checks=checks)
        assert result["preflight_ok"] == 1
        assert result["preflight_issues"] == 1


# ---------------------------------------------------------------------------
# Bulk action unit tests (via API client)
# ---------------------------------------------------------------------------


class TestBulkActionAPI:
    def _setup(self, tmp_path: Path, monkeypatch):
        _skip_no_fastapi()
        import zen_claw.config.loader as loader_mod

        ws = tmp_path / "ws"
        _make_skill_dir(ws, "skill_a", enabled=False)
        _make_skill_dir(ws, "skill_b", enabled=False)
        _make_skill_dir(ws, "skill_c", enabled=True)

        monkeypatch.setattr(loader_mod, "get_data_dir", lambda: tmp_path)
        monkeypatch.setattr(loader_mod, "load_config", lambda: _fake_config(ws))
        monkeypatch.setattr(_srv_mod, "_api_keys_file", lambda: tmp_path / "api_keys.json")
        raw, _ = generate_api_key()
        store_api_key(raw)
        return ws, raw

    def test_bulk_enable_action(self, tmp_path: Path, monkeypatch) -> None:
        """bulk-action enable 2 skills → succeeded=2."""
        ws, raw = self._setup(tmp_path, monkeypatch)
        client = _TestClient(api_app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/skills/bulk-action",
            json={"names": ["skill_a", "skill_b"], "action": "enable"},
            headers={"X-API-Key": raw},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["succeeded"] == 2
        assert data["failed"] == 0
        assert data["action"] == "enable"
        assert data["reload_required"] is False
        assert data["apply_state"]["target"] == "bulk:enable"
        assert data["apply_state"]["execution_mode"] == "enable"

    def test_bulk_disable_action(self, tmp_path: Path, monkeypatch) -> None:
        """bulk-action disable 1 skill → skill becomes disabled."""
        ws, raw = self._setup(tmp_path, monkeypatch)
        client = _TestClient(api_app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/skills/bulk-action",
            json={"names": ["skill_c"], "action": "disable"},
            headers={"X-API-Key": raw},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["succeeded"] == 1
        assert data["apply_state"]["execution_mode"] == "disable"

    def test_bulk_preflight_action(self, tmp_path: Path, monkeypatch) -> None:
        """bulk-action preflight → results contain ok field."""
        ws, raw = self._setup(tmp_path, monkeypatch)
        client = _TestClient(api_app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/skills/bulk-action",
            json={"names": ["skill_a"], "action": "preflight"},
            headers={"X-API-Key": raw},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 1
        assert "ok" in data["results"][0]
        assert data["apply_state"]["target"] == "bulk:preflight"
        assert data["apply_state"]["execution_mode"] == "preflight"

    def test_bulk_action_dry_run_enable(self, tmp_path: Path, monkeypatch) -> None:
        """dry_run=True → returns ok but skill state unchanged."""
        ws, raw = self._setup(tmp_path, monkeypatch)
        loader = _make_loader(ws)
        was_enabled = loader.is_skill_enabled("skill_a")

        client = _TestClient(api_app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/skills/bulk-action",
            json={"names": ["skill_a"], "action": "enable", "dry_run": True},
            headers={"X-API-Key": raw},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["dry_run"] is True
        assert data["apply_state"]["execution_mode"] == "preview"
        # Skill state should not have changed (dry_run)
        assert loader.is_skill_enabled("skill_a") == was_enabled

    def test_bulk_action_too_many_names(self, tmp_path: Path, monkeypatch) -> None:
        """names > 20 → 400."""
        _skip_no_fastapi()
        import zen_claw.config.loader as loader_mod

        ws = tmp_path / "ws2"
        ws.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(loader_mod, "get_data_dir", lambda: tmp_path)
        monkeypatch.setattr(loader_mod, "load_config", lambda: _fake_config(ws))
        monkeypatch.setattr(_srv_mod, "_api_keys_file", lambda: tmp_path / "api_keys.json")
        raw, _ = generate_api_key()
        store_api_key(raw)

        client = _TestClient(api_app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/skills/bulk-action",
            json={"names": [f"s{i}" for i in range(21)], "action": "enable"},
            headers={"X-API-Key": raw},
        )
        assert resp.status_code == 400

    def test_bulk_action_invalid_action(self, tmp_path: Path, monkeypatch) -> None:
        """action='delete' → 400."""
        _skip_no_fastapi()
        import zen_claw.config.loader as loader_mod

        ws = tmp_path / "ws3"
        ws.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(loader_mod, "get_data_dir", lambda: tmp_path)
        monkeypatch.setattr(loader_mod, "load_config", lambda: _fake_config(ws))
        monkeypatch.setattr(_srv_mod, "_api_keys_file", lambda: tmp_path / "api_keys.json")
        raw, _ = generate_api_key()
        store_api_key(raw)

        client = _TestClient(api_app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/skills/bulk-action",
            json={"names": ["a"], "action": "delete"},
            headers={"X-API-Key": raw},
        )
        assert resp.status_code == 400

    def test_bulk_action_api_returns_200(self, tmp_path: Path, monkeypatch) -> None:
        """POST /api/v1/skills/bulk-action → 200 with results list."""
        ws, raw = self._setup(tmp_path, monkeypatch)
        client = _TestClient(api_app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/skills/bulk-action",
            json={"names": ["skill_a"], "action": "enable"},
            headers={"X-API-Key": raw},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data
        assert isinstance(data["results"], list)

    def test_bulk_action_api_unknown_skill(self, tmp_path: Path, monkeypatch) -> None:
        """Unknown skill in names → that result ok=False, others not affected."""
        ws, raw = self._setup(tmp_path, monkeypatch)
        client = _TestClient(api_app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/skills/bulk-action",
            json={"names": ["skill_a", "nonexistent_xyz"], "action": "enable"},
            headers={"X-API-Key": raw},
        )
        assert resp.status_code == 200
        data = resp.json()
        results_by_name = {r["name"]: r for r in data["results"]}
        assert results_by_name["skill_a"]["ok"] is True
        assert results_by_name["nonexistent_xyz"]["ok"] is False


# ---------------------------------------------------------------------------
# Health summary API tests
# ---------------------------------------------------------------------------


class TestHealthSummaryAPI:
    def test_health_summary_api_returns_200(self, tmp_path: Path, monkeypatch) -> None:
        """GET /api/v1/skills/health-summary → 200."""
        _skip_no_fastapi()
        import zen_claw.config.loader as loader_mod

        ws = tmp_path / "ws"
        _make_skill_dir(ws, "demo")
        monkeypatch.setattr(loader_mod, "get_data_dir", lambda: tmp_path)
        monkeypatch.setattr(loader_mod, "load_config", lambda: _fake_config(ws))
        monkeypatch.setattr(_srv_mod, "_api_keys_file", lambda: tmp_path / "api_keys.json")
        raw, _ = generate_api_key()
        store_api_key(raw)

        client = _TestClient(api_app, raise_server_exceptions=False)
        resp = client.get("/api/v1/skills/health-summary", headers={"X-API-Key": raw})
        assert resp.status_code == 200

    def test_health_summary_api_fields(self, tmp_path: Path, monkeypatch) -> None:
        """Response contains total/enabled/trusted fields."""
        _skip_no_fastapi()
        import zen_claw.config.loader as loader_mod

        ws = tmp_path / "ws"
        _make_skill_dir(ws, "demo2")
        monkeypatch.setattr(loader_mod, "get_data_dir", lambda: tmp_path)
        monkeypatch.setattr(loader_mod, "load_config", lambda: _fake_config(ws))
        monkeypatch.setattr(_srv_mod, "_api_keys_file", lambda: tmp_path / "api_keys.json")
        raw, _ = generate_api_key()
        store_api_key(raw)

        client = _TestClient(api_app, raise_server_exceptions=False)
        resp = client.get("/api/v1/skills/health-summary", headers={"X-API-Key": raw})
        data = resp.json()
        for field in ("total", "enabled", "manifest_ok", "trusted", "runtime_covered", "enabled_pct", "trusted_pct", "preflight_ok_pct"):
            assert field in data, f"missing field: {field}"

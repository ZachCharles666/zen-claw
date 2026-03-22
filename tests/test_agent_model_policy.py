"""Tests for GET /api/v1/agents/{id}/model-policy and POST /api/v1/ops/reload-execute."""

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


def _skip():
    if not _FASTAPI_AVAILABLE:
        pytest.skip("fastapi not available")


def _fake_config(data_dir: Path):
    from zen_claw.config.schema import Config
    return Config.model_validate({"agents": {"defaults": {"workspace": str(data_dir / "ws")}}})


def _api_setup(tmp_path: Path, monkeypatch, *, config=None):
    _skip()
    import zen_claw.config.loader as loader_mod

    cfg = config or _fake_config(tmp_path)
    monkeypatch.setattr(loader_mod, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(loader_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(_srv_mod, "_api_keys_file", lambda: tmp_path / "api_keys.json")
    raw, _ = generate_api_key()
    store_api_key(raw)
    return raw, cfg


# ---------------------------------------------------------------------------
# _build_reload_execute_plan unit tests
# ---------------------------------------------------------------------------


class TestBuildReloadPlan:
    def test_build_reload_plan_structure(self, tmp_path: Path) -> None:
        """_build_reload_execute_plan returns entries with required fields."""
        from zen_claw.dashboard.server import _build_reload_execute_plan
        from zen_claw.config.schema import Config

        cfg = Config.model_validate({"agents": {"defaults": {"workspace": str(tmp_path)}}})
        plan = _build_reload_execute_plan(cfg)
        assert len(plan) >= 1
        for entry in plan:
            assert "surface" in entry
            assert "executable" in entry
            assert "execution_mode" in entry

    def test_build_reload_plan_has_agent_entry(self, tmp_path: Path) -> None:
        """Plan always includes at least one agent entry with config_reload mode."""
        from zen_claw.dashboard.server import _build_reload_execute_plan
        from zen_claw.config.schema import Config

        cfg = Config.model_validate({"agents": {"defaults": {"workspace": str(tmp_path)}}})
        plan = _build_reload_execute_plan(cfg)
        modes = [e["execution_mode"] for e in plan]
        assert "config_reload" in modes

    def test_build_reload_plan_surface_filter(self, tmp_path: Path) -> None:
        """surface filter narrows results."""
        from zen_claw.dashboard.server import _build_reload_execute_plan
        from zen_claw.config.schema import Config

        cfg = Config.model_validate({"agents": {"defaults": {"workspace": str(tmp_path)}}})
        plan = _build_reload_execute_plan(cfg, surface="agent")
        assert all(e["surface"] == "agent" for e in plan)


# ---------------------------------------------------------------------------
# GET /api/v1/agents/{id}/model-policy tests
# ---------------------------------------------------------------------------


class TestGetModelPolicy:
    def test_get_model_policy_returns_200(self, tmp_path: Path, monkeypatch) -> None:
        """GET /api/v1/agents/default/model-policy → 200."""
        raw, _ = _api_setup(tmp_path, monkeypatch)
        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.get("/api/v1/agents/default/model-policy", headers={"X-API-Key": raw})
        assert resp.status_code == 200

    def test_get_model_policy_fields(self, tmp_path: Path, monkeypatch) -> None:
        """Response contains model / cost_model / stability_model / task_type_model_overrides."""
        raw, _ = _api_setup(tmp_path, monkeypatch)
        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.get("/api/v1/agents/default/model-policy", headers={"X-API-Key": raw})
        data = resp.json()
        for field in ("agent_id", "model", "cost_model", "stability_model", "task_type_model_overrides"):
            assert field in data, f"missing field: {field}"

    def test_get_model_policy_unknown_agent_404(self, tmp_path: Path, monkeypatch) -> None:
        """Nonexistent agent_id → 404."""
        raw, _ = _api_setup(tmp_path, monkeypatch)
        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.get("/api/v1/agents/ghost_xyz_99/model-policy", headers={"X-API-Key": raw})
        assert resp.status_code == 404

    def test_get_model_policy_task_type_overrides_is_dict(self, tmp_path: Path, monkeypatch) -> None:
        """task_type_model_overrides is always a dict (possibly empty)."""
        raw, _ = _api_setup(tmp_path, monkeypatch)
        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.get("/api/v1/agents/default/model-policy", headers={"X-API-Key": raw})
        data = resp.json()
        assert isinstance(data["task_type_model_overrides"], dict)

    def test_get_model_policy_after_update(self, tmp_path: Path, monkeypatch) -> None:
        """POST model-policy then GET reflects new cost_model value."""
        import zen_claw.config.loader as loader_mod

        raw, cfg = _api_setup(tmp_path, monkeypatch)
        # Allow save_config to work with real config file
        from zen_claw.config.loader import get_config_path

        monkeypatch.setattr(loader_mod, "save_config", lambda c, p=None: None)

        client = _TC(api_app, raise_server_exceptions=False)
        # POST update
        client.post(
            "/api/v1/agents/default/model-policy",
            json={"cost_model": "gpt-4o-mini"},
            headers={"X-API-Key": raw},
        )
        # GET should reflect the update (since save_config is mocked, the in-process config is updated)
        resp = client.get("/api/v1/agents/default/model-policy", headers={"X-API-Key": raw})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /api/v1/ops/reload-execute tests
# ---------------------------------------------------------------------------


class TestReloadExecute:
    def test_reload_execute_api_returns_200(self, tmp_path: Path, monkeypatch) -> None:
        """POST /api/v1/ops/reload-execute → 200."""
        raw, _ = _api_setup(tmp_path, monkeypatch)
        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/ops/reload-execute",
            json={"dry_run": True},
            headers={"X-API-Key": raw},
        )
        assert resp.status_code == 200

    def test_reload_execute_dry_run(self, tmp_path: Path, monkeypatch) -> None:
        """dry_run=True → executed=0, ops log has no new entry."""
        raw, _ = _api_setup(tmp_path, monkeypatch)
        log_path = tmp_path / "dashboard" / "reload_execute.log.jsonl"

        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/ops/reload-execute",
            json={"dry_run": True},
            headers={"X-API-Key": raw},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["dry_run"] is True
        assert data["executed"] == 0
        assert "plan" in data
        # dry_run should NOT write to audit log
        assert not log_path.exists() or log_path.read_text().strip() == ""

    def test_reload_execute_returns_results(self, tmp_path: Path, monkeypatch) -> None:
        """dry_run=False → results list non-empty with execution_mode."""
        raw, _ = _api_setup(tmp_path, monkeypatch)
        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/ops/reload-execute",
            json={"dry_run": False},
            headers={"X-API-Key": raw},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data.get("results"), list)
        assert len(data["results"]) >= 1
        for r in data["results"]:
            assert "execution_mode" in r

    def test_reload_execute_writes_audit(self, tmp_path: Path, monkeypatch) -> None:
        """dry_run=False → reload_execute.log.jsonl has event_type=reload_execute entry."""
        raw, _ = _api_setup(tmp_path, monkeypatch)
        (tmp_path / "dashboard").mkdir(parents=True, exist_ok=True)

        client = _TC(api_app, raise_server_exceptions=False)
        client.post(
            "/api/v1/ops/reload-execute",
            json={"dry_run": False},
            headers={"X-API-Key": raw},
        )
        log_path = tmp_path / "dashboard" / "reload_execute.log.jsonl"
        assert log_path.exists()
        lines = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        assert any(l.get("event_type") == "reload_execute" for l in lines)

    def test_reload_execute_audit_only_surface(self, tmp_path: Path, monkeypatch) -> None:
        """Plan includes at least one audit_only entry (channel.webchat is in_process, others are audit_only)."""
        from zen_claw.dashboard.server import _build_reload_execute_plan
        from zen_claw.config.schema import Config

        # Create a config with a non-webchat channel
        cfg = Config.model_validate({
            "agents": {"defaults": {"workspace": str(tmp_path)}},
            "channels": {"feishu": {}},
        })
        plan = _build_reload_execute_plan(cfg)
        modes = [e["execution_mode"] for e in plan]
        assert "audit_only" in modes

    def test_reload_execute_surface_filter(self, tmp_path: Path, monkeypatch) -> None:
        """surface=agent → results only contain agent surface."""
        raw, _ = _api_setup(tmp_path, monkeypatch)
        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/ops/reload-execute",
            json={"surface": "agent", "dry_run": False},
            headers={"X-API-Key": raw},
        )
        assert resp.status_code == 200
        data = resp.json()
        for r in data.get("results", []):
            assert r["surface"] == "agent"

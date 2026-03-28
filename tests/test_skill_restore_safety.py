"""Tests for Skill export hash storage and restore rollback safety."""

from __future__ import annotations

import hashlib
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


def _make_skill_dir(root: Path, name: str) -> Path:
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"# {name}\nA test skill.")
    (skill_dir / "manifest.json").write_text(
        json.dumps({"name": name, "version": "1.0.0", "description": "test"})
    )
    return skill_dir


def _make_loader(workspace: Path):
    from zen_claw.agent.skills import SkillsLoader

    return SkillsLoader(workspace)


def _export(workspace: Path, name: str, out_zip: Path):
    loader = _make_loader(workspace)
    return loader.export_skill_to_zip(name, out_zip)


# ---------------------------------------------------------------------------
# Unit tests — export_skill_to_zip returns hash
# ---------------------------------------------------------------------------


class TestExportReturnsHash:
    def test_export_returns_three_tuple(self, tmp_path: Path) -> None:
        """export_skill_to_zip must return (ok, msg, sha256_hex)."""
        ws = tmp_path / "ws"
        _make_skill_dir(ws, "alpha")
        out_zip = tmp_path / "alpha.zip"
        result = _make_loader(ws).export_skill_to_zip("alpha", out_zip)
        assert len(result) == 3
        ok, msg, sha = result
        assert ok is True
        assert sha != ""
        assert len(sha) == 64  # SHA-256 hex digest

    def test_export_hash_matches_zip_content(self, tmp_path: Path) -> None:
        """Returned hash must equal sha256(zip bytes)."""
        ws = tmp_path / "ws"
        _make_skill_dir(ws, "alpha")
        out_zip = tmp_path / "alpha.zip"
        ok, _, sha = _make_loader(ws).export_skill_to_zip("alpha", out_zip)
        assert ok
        expected = hashlib.sha256(out_zip.read_bytes()).hexdigest()
        assert sha == expected

    def test_export_failure_returns_empty_hash(self, tmp_path: Path) -> None:
        """On failure, hash must be empty string."""
        ws = tmp_path / "ws"
        ws.mkdir(parents=True)
        loader = _make_loader(ws)
        ok, msg, sha = loader.export_skill_to_zip("nonexistent", tmp_path / "nope.zip")
        assert ok is False
        assert sha == ""


# ---------------------------------------------------------------------------
# Audit log stores export_hash
# ---------------------------------------------------------------------------


class TestExportHashInAuditLog:
    def test_export_hash_stored_in_audit_log(self, tmp_path: Path) -> None:
        """After export, skills_exports.log.jsonl must contain export_hash."""
        from zen_claw.dashboard.server import _append_skill_export_audit

        ws = tmp_path / "ws"
        _make_skill_dir(ws, "beta")
        out_zip = tmp_path / "beta.zip"
        ok, msg, sha = _make_loader(ws).export_skill_to_zip("beta", out_zip)
        assert ok

        _append_skill_export_audit(
            data_dir=tmp_path,
            skill_name="beta",
            out_path=out_zip,
            message=msg,
            actor="test",
            export_hash=sha,
        )

        log_path = tmp_path / "dashboard" / "skills_exports.log.jsonl"
        assert log_path.exists()
        rows = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        assert rows, "audit log should not be empty"
        assert rows[-1]["export_hash"] == sha

    def test_export_hash_empty_string_when_not_provided(self, tmp_path: Path) -> None:
        """_append_skill_export_audit without export_hash stores empty string."""
        from zen_claw.dashboard.server import _append_skill_export_audit

        _append_skill_export_audit(
            data_dir=tmp_path,
            skill_name="gamma",
            out_path=tmp_path / "gamma.zip",
            message="test",
            actor="test",
        )
        log_path = tmp_path / "dashboard" / "skills_exports.log.jsonl"
        rows = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        assert rows[-1]["export_hash"] == ""


# ---------------------------------------------------------------------------
# _build_skill_restore_plan — hash verification
# ---------------------------------------------------------------------------


class TestRestorePlanHashVerification:
    def _make_plan(self, tmp_path: Path, *, tamper: bool = False, has_stored_hash: bool = True):
        """Helper to build a restore plan with or without hash mismatch."""
        from zen_claw.dashboard.server import (
            _append_skill_export_audit,
            _build_skill_restore_plan,
        )

        ws = tmp_path / "ws"
        _make_skill_dir(ws, "delta")
        out_zip = tmp_path / "delta.zip"
        ok, msg, sha = _make_loader(ws).export_skill_to_zip("delta", out_zip)
        assert ok

        # Write audit entry with or without hash.
        _append_skill_export_audit(
            data_dir=tmp_path,
            skill_name="delta",
            out_path=out_zip,
            message=msg,
            actor="test",
            export_hash=sha if has_stored_hash else "",
        )

        if tamper:
            # Corrupt the zip bytes.
            original = out_zip.read_bytes()
            out_zip.write_bytes(original[:-4] + b"\xff\xff\xff\xff")

        package_state = {
            "latest_export_path": str(out_zip),
            "selected_physical_dir": "delta",
            "package_rows": [{"physical_dir": "delta", "version": "1.0.0"}],
        }
        package_policy: dict = {
            "preferred_physical_dir": "",
            "preferred_version": "",
            "retention_hours": 24,
            "require_export_for_restore": True,
            "warnings": [],
        }
        return _build_skill_restore_plan(
            data_dir=tmp_path,
            skill_name="delta",
            package_state=package_state,
            package_policy=package_policy,
        )

    def test_restore_plan_hash_verified_ok(self, tmp_path: Path) -> None:
        plan = self._make_plan(tmp_path, tamper=False)
        assert plan["hash_verified"] is True
        assert not any("hash mismatch" in r for r in plan["blocked_reasons"])
        assert plan["ready"] is True

    def test_restore_plan_hash_mismatch_blocked(self, tmp_path: Path) -> None:
        plan = self._make_plan(tmp_path, tamper=True)
        assert plan["hash_verified"] is False
        assert any("hash mismatch" in r for r in plan["blocked_reasons"])
        assert plan["ready"] is False

    def test_restore_plan_no_stored_hash_graceful(self, tmp_path: Path) -> None:
        """Old export events without export_hash must not block restore."""
        plan = self._make_plan(tmp_path, has_stored_hash=False)
        assert plan["hash_verified"] is True
        assert not any("hash mismatch" in r for r in plan["blocked_reasons"])
        assert plan["export_hash"] == ""

    def test_restore_plan_includes_hash_fields(self, tmp_path: Path) -> None:
        plan = self._make_plan(tmp_path)
        assert "export_hash" in plan
        assert "current_zip_hash" in plan
        assert "hash_verified" in plan


# ---------------------------------------------------------------------------
# _restore_skill_with_policy — blocked on hash mismatch + integrity check
# ---------------------------------------------------------------------------


class TestRestoreWithPolicy:
    def _setup(self, tmp_path: Path, tamper: bool = False):
        """Build a workspace with a skill and a (possibly tampered) export."""
        from zen_claw.dashboard.server import _append_skill_export_audit

        ws = tmp_path / "ws"
        _make_skill_dir(ws, "epsilon")
        out_zip = tmp_path / "exports" / "epsilon.zip"
        out_zip.parent.mkdir(parents=True, exist_ok=True)
        ok, msg, sha = _make_loader(ws).export_skill_to_zip("epsilon", out_zip)
        assert ok

        _append_skill_export_audit(
            data_dir=tmp_path,
            skill_name="epsilon",
            out_path=out_zip,
            message=msg,
            actor="test",
            export_hash=sha,
        )

        if tamper:
            original = out_zip.read_bytes()
            out_zip.write_bytes(original[:-4] + b"\xde\xad\xbe\xef")

        return ws, out_zip

    def test_restore_blocked_when_hash_mismatch(self, tmp_path: Path, monkeypatch) -> None:
        """_restore_skill_with_policy must raise 409 when zip is tampered."""
        from fastapi import HTTPException

        import zen_claw.config.loader as loader_mod
        import zen_claw.dashboard.server as srv

        ws, _ = self._setup(tmp_path, tamper=True)

        monkeypatch.setattr(loader_mod, "get_data_dir", lambda: tmp_path)
        monkeypatch.setattr(loader_mod, "load_config", lambda: _fake_config(ws))

        with pytest.raises(HTTPException) as exc_info:
            srv._restore_skill_with_policy(name="epsilon")
        assert exc_info.value.status_code == 409
        assert "hash mismatch" in str(exc_info.value.detail)

    def test_restore_includes_integrity_check(self, tmp_path: Path, monkeypatch) -> None:
        """Successful restore response must include integrity_check field."""
        import zen_claw.config.loader as loader_mod
        import zen_claw.dashboard.server as srv

        ws, _ = self._setup(tmp_path, tamper=False)

        monkeypatch.setattr(loader_mod, "get_data_dir", lambda: tmp_path)
        monkeypatch.setattr(loader_mod, "load_config", lambda: _fake_config(ws))

        resp = srv._restore_skill_with_policy(name="epsilon")
        data = json.loads(resp.body)
        assert "integrity_check" in data
        assert "ok" in data["integrity_check"]
        assert "errors" in data["integrity_check"]


def _fake_config(workspace: Path):
    """Create a minimal Config with workspace set to *workspace*."""
    from zen_claw.config.schema import Config

    return Config.model_validate({"agents": {"defaults": {"workspace": str(workspace)}}})


# ---------------------------------------------------------------------------
# API tests
# ---------------------------------------------------------------------------


def _skip_no_fastapi():
    if not _FASTAPI_AVAILABLE:
        pytest.skip("fastapi not available")


class TestExportRestoreAPI:
    def test_export_api_includes_hash(self, tmp_path: Path, monkeypatch) -> None:
        """POST /api/v1/skills/{name}/export response must include export_hash."""
        _skip_no_fastapi()

        ws = tmp_path / "ws"
        _make_skill_dir(ws, "zeta")

        import zen_claw.config.loader as loader_mod

        monkeypatch.setattr(loader_mod, "get_data_dir", lambda: tmp_path)
        monkeypatch.setattr(loader_mod, "load_config", lambda: _fake_config(ws))
        monkeypatch.setattr(_srv_mod, "_api_keys_file", lambda: tmp_path / "api_keys.json")
        raw, _ = generate_api_key()
        store_api_key(raw)

        client = _TestClient(api_app, raise_server_exceptions=False)
        resp = client.post("/api/v1/skills/zeta/export", headers={"X-API-Key": raw})
        assert resp.status_code == 200
        data = resp.json()
        assert "export_hash" in data
        assert len(data["export_hash"]) == 64

    def test_restore_plan_api_includes_hash_fields(self, tmp_path: Path, monkeypatch) -> None:
        """GET /api/v1/skills/{name}/restore-plan must include hash_verified field."""
        _skip_no_fastapi()

        import zen_claw.config.loader as loader_mod
        from zen_claw.dashboard.server import _append_skill_export_audit

        ws = tmp_path / "ws"
        _make_skill_dir(ws, "eta")
        out_zip = ws / ".zen-claw" / "exports" / "eta.zip"
        out_zip.parent.mkdir(parents=True, exist_ok=True)
        ok, msg, sha = _make_loader(ws).export_skill_to_zip("eta", out_zip)
        assert ok
        _append_skill_export_audit(
            data_dir=tmp_path,
            skill_name="eta",
            out_path=out_zip,
            message=msg,
            actor="test",
            export_hash=sha,
        )

        monkeypatch.setattr(loader_mod, "get_data_dir", lambda: tmp_path)
        monkeypatch.setattr(loader_mod, "load_config", lambda: _fake_config(ws))
        monkeypatch.setattr(_srv_mod, "_api_keys_file", lambda: tmp_path / "api_keys.json")
        raw, _ = generate_api_key()
        store_api_key(raw)

        client = _TestClient(api_app, raise_server_exceptions=False)
        resp = client.get("/api/v1/skills/eta/restore-plan", headers={"X-API-Key": raw})
        assert resp.status_code == 200
        data = resp.json()
        assert "hash_verified" in data or "restore_plan" in data  # either direct or nested

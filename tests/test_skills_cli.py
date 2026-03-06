import hashlib
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from zen_claw.agent.skills import SkillsLoader
from zen_claw.cli.commands import app


@pytest.fixture(autouse=True)
def mock_skills_loader(monkeypatch):
    # Mock mapping and time to prevent potentially slow/hanging I/O or crypto in CI
    monkeypatch.setattr(SkillsLoader, "_load_skill_mapping", lambda self: None)
    monkeypatch.setattr(SkillsLoader, "_save_skill_mapping", lambda self: None)
    monkeypatch.setattr(SkillsLoader, "_now_ts", lambda self: 1000.0)


def _write_skill(workspace: Path, name: str, manifest: str | None = None) -> None:
    skill_dir = workspace / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    if manifest is not None:
        (skill_dir / "manifest.json").write_text(manifest, encoding="utf-8")


def _patch_workspace(monkeypatch, workspace: Path, builtin: Path) -> None:
    cfg = SimpleNamespace(
        workspace_path=workspace,
        tools=SimpleNamespace(policy=SimpleNamespace(production_hardening=False)),
        agents=SimpleNamespace(defaults=SimpleNamespace(skill_permissions_mode="off")),
    )
    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda: cfg)
    monkeypatch.setattr("zen_claw.agent.skills.BUILTIN_SKILLS_DIR", builtin)


def test_skills_cli_enable_disable_roundtrip(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    _write_skill(workspace, "alpha")
    _patch_workspace(monkeypatch, workspace, builtin)

    out_disable = runner.invoke(app, ["skills", "disable", "alpha"])
    assert out_disable.exit_code == 0
    assert "Disabled skill: alpha" in out_disable.output

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    assert loader.is_skill_enabled("alpha") is False

    out_enable = runner.invoke(app, ["skills", "enable", "alpha"])
    assert out_enable.exit_code == 0
    assert "Enabled skill: alpha" in out_enable.output
    assert loader.is_skill_enabled("alpha") is True


def test_skills_cli_validate_strict_fails_without_manifest(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    _write_skill(workspace, "alpha")
    _patch_workspace(monkeypatch, workspace, builtin)

    out = runner.invoke(app, ["skills", "validate", "--name", "alpha", "--strict"])
    assert out.exit_code == 1
    assert "alpha: invalid" in out.output
    assert "manifest.json missing" in out.output


def test_skills_cli_validate_all_success(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    _write_skill(
        workspace,
        "alpha",
        json.dumps(
            {
                "name": "alpha",
                "version": "1.0.0",
                "description": "alpha skill",
                "permissions": ["web_search"],
            }
        ),
    )
    _patch_workspace(monkeypatch, workspace, builtin)

    out = runner.invoke(app, ["skills", "validate", "--strict"])
    assert out.exit_code == 0
    assert "All skill manifests are valid" in out.output


def test_skills_cli_validate_with_integrity_success(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    skill_dir = workspace / "skills" / "alpha"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text("# alpha\n", encoding="utf-8")
    digest = hashlib.sha256(skill_md.read_bytes()).hexdigest()
    (skill_dir / "manifest.json").write_text(
        json.dumps(
            {
                "name": "alpha",
                "version": "1.0.0",
                "description": "alpha skill",
                "permissions": ["web_search"],
                "integrity": {"SKILL.md": f"sha256:{digest}"},
            }
        ),
        encoding="utf-8",
    )
    _patch_workspace(monkeypatch, workspace, builtin)

    out = runner.invoke(app, ["skills", "validate", "--strict", "--integrity"])
    assert out.exit_code == 0
    assert "All skill manifests are valid" in out.output


def test_skills_cli_validate_with_integrity_mismatch_fails(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    skill_dir = workspace / "skills" / "alpha"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("# alpha\n", encoding="utf-8")
    (skill_dir / "manifest.json").write_text(
        json.dumps(
            {
                "name": "alpha",
                "version": "1.0.0",
                "description": "alpha skill",
                "permissions": ["web_search"],
                "integrity": {"SKILL.md": "sha256:" + ("0" * 64)},
            }
        ),
        encoding="utf-8",
    )
    _patch_workspace(monkeypatch, workspace, builtin)

    out = runner.invoke(app, ["skills", "validate", "--strict", "--integrity"])
    assert out.exit_code == 1
    assert "integrity mismatch: SKILL.md" in out.output


def test_skills_cli_verify_integrity_success(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    skill_dir = workspace / "skills" / "alpha"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text("# alpha\n", encoding="utf-8")
    digest = hashlib.sha256(skill_md.read_bytes()).hexdigest()
    (skill_dir / "manifest.json").write_text(
        json.dumps(
            {
                "name": "alpha",
                "version": "1.0.0",
                "description": "alpha",
                "permissions": ["read_file"],
                "integrity": {"SKILL.md": f"sha256:{digest}"},
            }
        ),
        encoding="utf-8",
    )
    _patch_workspace(monkeypatch, workspace, builtin)

    out = runner.invoke(app, ["skills", "verify-integrity", "--name", "alpha"])
    assert out.exit_code == 0
    assert "integrity valid" in out.output


def test_skills_cli_verify_integrity_detects_mismatch(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    skill_dir = workspace / "skills" / "alpha"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("# alpha\nchanged\n", encoding="utf-8")
    (skill_dir / "manifest.json").write_text(
        json.dumps(
            {
                "name": "alpha",
                "version": "1.0.0",
                "description": "alpha",
                "permissions": ["read_file"],
                "integrity": {"SKILL.md": "sha256:" + ("0" * 64)},
            }
        ),
        encoding="utf-8",
    )
    _patch_workspace(monkeypatch, workspace, builtin)

    out = runner.invoke(app, ["skills", "verify-integrity", "--name", "alpha"])
    assert out.exit_code == 1
    assert "integrity mismatch: SKILL.md" in out.output


def test_skills_cli_verify_integrity_require_mode_fails_when_missing(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    _write_skill(
        workspace,
        "alpha",
        json.dumps(
            {
                "name": "alpha",
                "version": "1.0.0",
                "description": "alpha",
                "permissions": ["read_file"],
            }
        ),
    )
    _patch_workspace(monkeypatch, workspace, builtin)

    out = runner.invoke(
        app, ["skills", "verify-integrity", "--name", "alpha", "--require-integrity"]
    )
    assert out.exit_code == 1
    assert "integrity missing in manifest.json" in out.output


def test_skills_cli_install_and_uninstall(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    source = tmp_path / "source_skill"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("# gamma\n", encoding="utf-8")
    _patch_workspace(monkeypatch, workspace, builtin)

    out_install = runner.invoke(app, ["skills", "install", str(source), "--name", "gamma"])
    assert out_install.exit_code == 0
    assert "installed skill: gamma" in out_install.output
    assert (workspace / "skills" / "gamma" / "SKILL.md").exists()

    out_uninstall = runner.invoke(app, ["skills", "uninstall", "gamma"])
    assert out_uninstall.exit_code == 0
    assert "uninstalled skill: gamma" in out_uninstall.output
    assert not (workspace / "skills" / "gamma").exists()


def test_skills_cli_install_fails_on_missing_skill_md(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    source = tmp_path / "source_skill"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    source.mkdir(parents=True)
    _patch_workspace(monkeypatch, workspace, builtin)

    out = runner.invoke(app, ["skills", "install", str(source)])
    assert out.exit_code == 1
    assert "must contain SKILL.md" in out.output


def test_skills_lifecycle_end_to_end_via_cli(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    source = tmp_path / "echo_skill"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("# echo_skill\nalways do X\n", encoding="utf-8")
    _patch_workspace(monkeypatch, workspace, builtin)

    out_install = runner.invoke(app, ["skills", "install", str(source)])
    assert out_install.exit_code == 0

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    content_enabled = loader.load_skills_for_context(["echo_skill"])
    assert "Skill: echo_skill" in content_enabled

    out_disable = runner.invoke(app, ["skills", "disable", "echo_skill"])
    assert out_disable.exit_code == 0
    content_disabled = loader.load_skills_for_context(["echo_skill"])
    assert content_disabled == ""

    out_uninstall = runner.invoke(app, ["skills", "uninstall", "echo_skill"])
    assert out_uninstall.exit_code == 0
    assert not (workspace / "skills" / "echo_skill").exists()


def test_skills_cli_install_strict_manifest_requires_manifest(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    source = tmp_path / "strict_skill"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("# strict_skill\n", encoding="utf-8")
    _patch_workspace(monkeypatch, workspace, builtin)

    out = runner.invoke(app, ["skills", "install", str(source), "--strict-manifest"])
    assert out.exit_code == 1
    assert "manifest.json missing" in out.output


def test_skills_cli_info_shows_manifest_details(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    _write_skill(
        workspace,
        "alpha",
        json.dumps(
            {
                "name": "alpha",
                "version": "1.2.3",
                "description": "alpha skill",
                "permissions": ["web_search", "read_file"],
                "scopes": ["network", "filesystem"],
            }
        ),
    )
    _patch_workspace(monkeypatch, workspace, builtin)

    out = runner.invoke(app, ["skills", "info", "alpha"])
    assert out.exit_code == 0
    assert "Skill: alpha" in out.output
    assert "manifest_version" in out.output
    assert "1.2.3" in out.output
    assert "web_search, read_file" in out.output
    assert "network, filesystem" in out.output
    normalized = "".join(out.output.split())
    assert "skillPermsEffectiveIfLoaded" in normalized
    assert "skillPermsEnforceReady" in normalized


def test_skills_cli_list_shows_manifest_status(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    _write_skill(
        workspace,
        "with_manifest",
        '{"name":"with_manifest","version":"1.0.0","description":"x","permissions":["read_file"],"scopes":["filesystem"]}',
    )
    _write_skill(workspace, "without_manifest")
    _patch_workspace(monkeypatch, workspace, builtin)

    out = runner.invoke(app, ["skills", "list", "--all", "--json"])
    assert out.exit_code == 0
    data = json.loads(out.output)
    by_name = {d["name"]: d for d in data}
    assert by_name["with_manifest"]["manifest"] == "valid"
    assert by_name["with_manifest"]["enforce_ready"] is True
    assert by_name["with_manifest"]["permissions_count"] == 1
    assert by_name["with_manifest"]["scopes_count"] == 1
    assert by_name["without_manifest"]["manifest"] == "missing"
    assert by_name["without_manifest"]["enforce_ready"] is False
    assert by_name["without_manifest"]["permissions_count"] == 0
    assert by_name["without_manifest"]["scopes_count"] == 0


def test_skills_cli_list_only_enforce_ready_filters(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    _write_skill(
        workspace,
        "ready",
        '{"name":"ready","version":"1.0.0","description":"x","permissions":["read_file"]}',
    )
    _write_skill(workspace, "not_ready_no_manifest")
    _patch_workspace(monkeypatch, workspace, builtin)

    out = runner.invoke(app, ["skills", "list", "--all", "--json", "--only-enforce-ready"])
    assert out.exit_code == 0
    data = json.loads(out.output)
    names = {d["name"] for d in data}
    assert names == {"ready"}


def test_skills_cli_install_overwrite(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    source = tmp_path / "alpha"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("# alpha v1\n", encoding="utf-8")
    (source / "manifest.json").write_text(
        '{"name":"alpha","version":"1.0.0","description":"x","permissions":["read_file"]}',
        encoding="utf-8",
    )
    _patch_workspace(monkeypatch, workspace, builtin)

    out1 = runner.invoke(app, ["skills", "install", str(source)])
    assert out1.exit_code == 0

    (source / "SKILL.md").write_text("# alpha v2\n", encoding="utf-8")
    out2 = runner.invoke(app, ["skills", "install", str(source)])
    assert out2.exit_code == 1
    assert "skill already exists" in out2.output

    out3 = runner.invoke(app, ["skills", "install", str(source), "--overwrite"])
    assert out3.exit_code == 0


def test_skills_cli_install_from_zip(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    source = tmp_path / "zip_skill"
    zip_path = tmp_path / "zip_skill.zip"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("# zip_skill\n", encoding="utf-8")
    (source / "manifest.json").write_text(
        '{"name":"zip_skill","version":"1.0.0","description":"x","permissions":["read_file"]}',
        encoding="utf-8",
    )
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(source / "SKILL.md", arcname="zip_skill/SKILL.md")
        zf.write(source / "manifest.json", arcname="zip_skill/manifest.json")
    _patch_workspace(monkeypatch, workspace, builtin)

    out = runner.invoke(app, ["skills", "install", str(zip_path)])
    assert out.exit_code == 0
    assert "installed skill: zip_skill" in out.output


def test_skills_cli_export_then_install(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    _write_skill(
        workspace,
        "alpha",
        '{"name":"alpha","version":"1.0.0","description":"x","permissions":["read_file"]}',
    )
    _patch_workspace(monkeypatch, workspace, builtin)

    out_zip = tmp_path / "exports" / "alpha.zip"
    out_export = runner.invoke(app, ["skills", "export", "alpha", "--out", str(out_zip)])
    assert out_export.exit_code == 0
    assert "sha256=" in out_export.output
    assert out_zip.exists()

    # remove then install from exported zip
    out_uninstall = runner.invoke(app, ["skills", "uninstall", "alpha"])
    assert out_uninstall.exit_code == 0
    out_install = runner.invoke(app, ["skills", "install", str(out_zip)])
    assert out_install.exit_code == 0


def test_skills_cli_sbom_json_output(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    _write_skill(
        workspace,
        "alpha",
        '{"name":"alpha","version":"1.0.0","description":"x","permissions":["read_file"],"trust":"trusted"}',
    )
    _patch_workspace(monkeypatch, workspace, builtin)

    out = runner.invoke(app, ["skills", "sbom"])
    assert out.exit_code == 0
    data = json.loads(out.output)
    assert data["schema"] == "zen-claw.skills.sbom.v1"
    assert data["skills_count"] == 1
    assert data["skills"][0]["name"] == "alpha"
    assert data["skills"][0]["manifest_status"] == "valid"
    file_paths = {f["path"] for f in data["skills"][0]["files"]}
    assert "SKILL.md" in file_paths
    assert "manifest.json" in file_paths


def test_skills_cli_sbom_out_file(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    _write_skill(workspace, "alpha")
    _patch_workspace(monkeypatch, workspace, builtin)

    out_file = tmp_path / "sbom" / "skills.json"
    out = runner.invoke(app, ["skills", "sbom", "--out", str(out_file)])
    assert out.exit_code == 0
    assert out_file.exists()
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["schema"] == "zen-claw.skills.sbom.v1"


def test_skills_cli_install_dry_run(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    source = tmp_path / "alpha"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("# alpha\n", encoding="utf-8")
    (source / "manifest.json").write_text(
        '{"name":"alpha","version":"1.0.0","description":"x","permissions":["read_file"]}',
        encoding="utf-8",
    )
    _patch_workspace(monkeypatch, workspace, builtin)

    out = runner.invoke(app, ["skills", "install", str(source), "--dry-run"])
    assert out.exit_code == 0
    assert "dry-run ok" in out.output
    assert not (workspace / "skills" / "alpha").exists()

import zipfile
from pathlib import Path

import pytest

from zen_claw.agent.skills import SkillsLoader


def _write_skill(workspace: Path, name: str, manifest: str | None = None) -> None:
    skill_dir = workspace / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    if manifest is not None:
        (skill_dir / "manifest.json").write_text(manifest, encoding="utf-8")


def test_skill_enable_disable_persists_state(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    _write_skill(workspace, "alpha")

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    assert loader.is_skill_enabled("alpha") is True
    assert loader.set_skill_enabled("alpha", False) is True
    assert loader.is_skill_enabled("alpha") is False

    loader2 = SkillsLoader(workspace, builtin_skills_dir=builtin)
    assert loader2.is_skill_enabled("alpha") is False
    assert loader2.set_skill_enabled("alpha", True) is True
    assert loader2.is_skill_enabled("alpha") is True


def test_list_skills_filters_disabled_when_filter_unavailable_true(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    _write_skill(workspace, "alpha")
    _write_skill(workspace, "beta")

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    assert loader.set_skill_enabled("beta", False) is True

    visible = {s["name"] for s in loader.list_skills(filter_unavailable=True)}
    all_skills = {s["name"] for s in loader.list_skills(filter_unavailable=False)}
    assert visible == {"alpha"}
    assert all_skills == {"alpha", "beta"}


def test_manifest_validation_strict_and_non_strict(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    _write_skill(workspace, "alpha")

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    ok_non_strict, errors_non_strict = loader.validate_skill_manifest("alpha", strict=False)
    ok_strict, errors_strict = loader.validate_skill_manifest("alpha", strict=True)

    assert ok_non_strict is True
    assert errors_non_strict == []
    assert ok_strict is False
    assert "manifest.json missing" in errors_strict


def test_manifest_validation_rejects_unknown_permissions(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    _write_skill(
        workspace,
        "alpha",
        """
{
  "name": "alpha",
  "version": "1.0.0",
  "description": "alpha skill",
  "permissions": ["web_search", "root_shell"]
}
""".strip(),
    )

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    ok, errors = loader.validate_skill_manifest("alpha", strict=True)
    assert ok is False
    assert any("permissions contains unknown entries" in e for e in errors)


def test_manifest_validation_rejects_duplicate_and_empty_permissions(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    _write_skill(
        workspace,
        "alpha",
        """
{
  "name": "alpha",
  "version": "1.0.0",
  "description": "alpha skill",
  "permissions": ["read_file", "read_file", " "]
}
""".strip(),
    )

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    ok, errors = loader.validate_skill_manifest("alpha", strict=True)
    assert ok is False
    assert any("permissions contains duplicate entries" in e for e in errors)
    assert any("permissions entries must be non-empty strings" in e for e in errors)


def test_manifest_validation_rejects_unknown_scopes(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    _write_skill(
        workspace,
        "alpha",
        """
{
  "name": "alpha",
  "version": "1.0.0",
  "description": "alpha skill",
  "permissions": ["read_file"],
  "scopes": ["network", "kernel"]
}
""".strip(),
    )

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    ok, errors = loader.validate_skill_manifest("alpha", strict=True)
    assert ok is False
    assert any("scopes contains unknown entries" in e for e in errors)


def test_manifest_validation_rejects_invalid_trust(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    _write_skill(
        workspace,
        "alpha",
        """
{
  "name": "alpha",
  "version": "1.0.0",
  "description": "alpha skill",
  "permissions": ["read_file"],
  "trust": "unknown"
}
""".strip(),
    )

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    ok, errors = loader.validate_skill_manifest("alpha", strict=True)
    assert ok is False
    assert any("trust must be one of" in e for e in errors)


def test_manifest_validation_rejects_duplicate_and_empty_scopes(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    _write_skill(
        workspace,
        "alpha",
        """
{
  "name": "alpha",
  "version": "1.0.0",
  "description": "alpha skill",
  "permissions": ["read_file"],
  "scopes": ["filesystem", "filesystem", " "]
}
""".strip(),
    )

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    ok, errors = loader.validate_skill_manifest("alpha", strict=True)
    assert ok is False
    assert any("scopes contains duplicate entries" in e for e in errors)
    assert any("scopes entries must be non-empty strings" in e for e in errors)


def test_manifest_validation_rejects_permission_scope_mismatch(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    _write_skill(
        workspace,
        "alpha",
        """
{
  "name": "alpha",
  "version": "1.0.0",
  "description": "alpha skill",
  "permissions": ["exec"],
  "scopes": ["filesystem"]
}
""".strip(),
    )

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    ok, errors = loader.validate_skill_manifest("alpha", strict=True)
    assert ok is False
    assert any("permissions not covered by scopes" in e for e in errors)


def test_validate_all_skill_manifests_returns_per_skill_results(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    _write_skill(
        workspace,
        "ok_skill",
        """
{
  "name": "ok_skill",
  "version": "1.2.3",
  "description": "good",
  "permissions": ["read_file"]
}
""".strip(),
    )
    _write_skill(
        workspace,
        "bad_skill",
        """
{
  "name": "bad_skill",
  "version": "v1",
  "description": ""
}
""".strip(),
    )

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    results = {item["name"]: item for item in loader.validate_all_skill_manifests(strict=True)}
    assert results["ok_skill"]["ok"] is True
    assert results["bad_skill"]["ok"] is False


def test_load_skills_for_context_skips_disabled_skills(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    _write_skill(workspace, "alpha")
    _write_skill(workspace, "beta")
    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    assert loader.set_skill_enabled("beta", False) is True

    loaded = loader.load_skills_for_context(["alpha", "beta"])
    assert "Skill: alpha" in loaded
    assert "Skill: beta" not in loaded


def test_install_and_uninstall_skill_from_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    source = tmp_path / "gamma"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("# gamma\n", encoding="utf-8")
    (source / "manifest.json").write_text(
        """
{
  "name": "gamma",
  "version": "1.0.0",
  "description": "gamma skill"
}
""".strip(),
        encoding="utf-8",
    )
    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)

    ok_install, msg_install = loader.install_skill_from_dir(source)
    assert ok_install is True
    assert "installed skill: gamma" in msg_install
    assert (workspace / "skills" / "gamma" / "SKILL.md").exists()

    ok_uninstall, msg_uninstall = loader.uninstall_skill("gamma")
    assert ok_uninstall is True
    assert "uninstalled skill: gamma" in msg_uninstall
    assert not (workspace / "skills" / "gamma").exists()


def test_install_skill_rejects_invalid_name_and_missing_skill_md(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    source = tmp_path / "source_skill"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    source.mkdir(parents=True)
    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)

    ok_missing, msg_missing = loader.install_skill_from_dir(source)
    assert ok_missing is False
    assert "must contain SKILL.md" in msg_missing

    (source / "SKILL.md").write_text("# x\n", encoding="utf-8")
    ok_name, msg_name = loader.install_skill_from_dir(source, name="../escape")
    assert ok_name is False
    assert "invalid skill name" in msg_name


def test_uninstall_builtin_skill_is_denied(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    workspace.mkdir(parents=True)
    builtin_skill = builtin / "core"
    builtin_skill.mkdir(parents=True)
    (builtin_skill / "SKILL.md").write_text("# core\n", encoding="utf-8")
    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)

    ok, msg = loader.uninstall_skill("core")
    assert ok is False
    assert "cannot uninstall built-in skill" in msg


def test_install_skill_strict_manifest_requires_manifest(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    source = tmp_path / "strict_skill"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("# strict_skill\n", encoding="utf-8")

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    ok, msg = loader.install_skill_from_dir(source, require_manifest=True)
    assert ok is False
    assert "manifest.json missing" in msg


def test_install_skill_rejects_manifest_with_duplicate_permissions(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    source = tmp_path / "dup_perm_skill"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("# dup_perm_skill\n", encoding="utf-8")
    (source / "manifest.json").write_text(
        """
{
  "name": "dup_perm_skill",
  "version": "1.0.0",
  "description": "dup perms",
  "permissions": ["read_file", "read_file"]
}
""".strip(),
        encoding="utf-8",
    )

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    ok, msg = loader.install_skill_from_dir(source, require_manifest=True)
    assert ok is False
    assert "duplicate entries" in msg


def test_install_skill_rejects_manifest_with_unknown_scopes(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    source = tmp_path / "bad_scope_skill"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("# bad_scope_skill\n", encoding="utf-8")
    (source / "manifest.json").write_text(
        """
{
  "name": "bad_scope_skill",
  "version": "1.0.0",
  "description": "bad scope",
  "permissions": ["read_file"],
  "scopes": ["network", "kernel"]
}
""".strip(),
        encoding="utf-8",
    )

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    ok, msg = loader.install_skill_from_dir(source, require_manifest=True)
    assert ok is False
    assert "scopes contains unknown entries" in msg


def test_validate_strict_requires_permissions_declaration(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    _write_skill(
        workspace,
        "alpha",
        """
{
  "name": "alpha",
  "version": "1.0.0",
  "description": "alpha skill"
}
""".strip(),
    )
    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    ok, errors = loader.validate_skill_manifest("alpha", strict=True)
    assert ok is False
    assert any("permissions must be declared" in e for e in errors)


def test_install_skill_overwrite_behavior(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    source = tmp_path / "alpha"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("# alpha v1\n", encoding="utf-8")
    (source / "manifest.json").write_text(
        '{"name":"alpha","version":"1.0.0","description":"a","permissions":["read_file"]}',
        encoding="utf-8",
    )
    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)

    ok1, _ = loader.install_skill_from_dir(source)
    assert ok1 is True

    # Update source content and verify overwrite gate.
    (source / "SKILL.md").write_text("# alpha v2\n", encoding="utf-8")
    ok2, msg2 = loader.install_skill_from_dir(source, overwrite=False)
    assert ok2 is False
    assert "skill already exists" in msg2

    ok3, _ = loader.install_skill_from_dir(source, overwrite=True)
    assert ok3 is True
    installed = (workspace / "skills" / "alpha" / "SKILL.md").read_text(encoding="utf-8")
    assert "v2" in installed


def test_install_skill_rejects_symlink_in_source(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    source = tmp_path / "alpha"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("# alpha\n", encoding="utf-8")
    (source / "manifest.json").write_text(
        '{"name":"alpha","version":"1.0.0","description":"a","permissions":["read_file"]}',
        encoding="utf-8",
    )
    target = source / "target.txt"
    target.write_text("x", encoding="utf-8")
    link = source / "link.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not available on this environment")

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    ok, msg = loader.install_skill_from_dir(source)
    assert ok is False
    assert "must not contain symlinks" in msg


def test_install_skill_from_zip(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    source = tmp_path / "zip_skill"
    zip_path = tmp_path / "zip_skill.zip"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("# zip_skill\n", encoding="utf-8")
    (source / "manifest.json").write_text(
        '{"name":"zip_skill","version":"1.0.0","description":"z","permissions":["read_file"]}',
        encoding="utf-8",
    )
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(source / "SKILL.md", arcname="zip_skill/SKILL.md")
        zf.write(source / "manifest.json", arcname="zip_skill/manifest.json")

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    ok, msg = loader.install_skill_from_zip(zip_path)
    assert ok is True
    assert "installed skill: zip_skill" in msg
    assert (workspace / "skills" / "zip_skill" / "SKILL.md").exists()


def test_install_skill_source_allowlist_blocks_unapproved_path(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    source = tmp_path / "alpha"
    allowed = tmp_path / "allowed"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    source.mkdir(parents=True)
    allowed.mkdir(parents=True)
    (source / "SKILL.md").write_text("# alpha\n", encoding="utf-8")
    (source / "manifest.json").write_text(
        '{"name":"alpha","version":"1.0.0","description":"a","permissions":["read_file"]}',
        encoding="utf-8",
    )
    monkeypatch.setenv("zen_claw_SKILL_INSTALL_ALLOWED_ROOTS", str(allowed))

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    ok, msg = loader.install_skill_from_dir(source)
    assert ok is False
    assert "source path not allowed by install allowlist" in msg


def test_install_skill_source_allowlist_allows_approved_path(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    allowed = tmp_path / "allowed"
    source = allowed / "alpha"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("# alpha\n", encoding="utf-8")
    (source / "manifest.json").write_text(
        '{"name":"alpha","version":"1.0.0","description":"a","permissions":["read_file"]}',
        encoding="utf-8",
    )
    monkeypatch.setenv("zen_claw_SKILL_INSTALL_ALLOWED_ROOTS", str(allowed))

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    ok, msg = loader.install_skill_from_dir(source)
    assert ok is True
    assert "installed skill: alpha" in msg


def test_install_skill_from_zip_rejects_multiple_skill_dirs(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    zip_path = tmp_path / "multi.zip"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("alpha/SKILL.md", "# alpha\n")
        zf.writestr("alpha/manifest.json", '{"name":"alpha","version":"1.0.0","description":"x"}')
        zf.writestr("beta/SKILL.md", "# beta\n")
        zf.writestr("beta/manifest.json", '{"name":"beta","version":"1.0.0","description":"x"}')

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    ok, msg = loader.install_skill_from_zip(zip_path)
    assert ok is False
    assert "exactly one skill directory" in msg


def test_install_skill_from_zip_rejects_path_traversal(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    zip_path = tmp_path / "bad.zip"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("../escape/SKILL.md", "# bad\n")
        zf.writestr("../escape/manifest.json", '{"name":"escape","version":"1.0.0","description":"x"}')

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    ok, msg = loader.install_skill_from_zip(zip_path)
    assert ok is False
    assert "invalid zip entry path" in msg


def test_install_skill_from_zip_rejects_too_many_files(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    zip_path = tmp_path / "too_many.zip"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bulk/SKILL.md", "# bulk\n")
        zf.writestr("bulk/manifest.json", '{"name":"bulk","version":"1.0.0","description":"x"}')
        for i in range(250):
            zf.writestr(f"bulk/files/{i}.txt", "x")

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    ok, msg = loader.install_skill_from_zip(zip_path)
    assert ok is False
    assert "too many files" in msg


def test_install_skill_from_zip_rejects_oversized_uncompressed_payload(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    zip_path = tmp_path / "too_large.zip"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)

    huge_payload = "a" * (11 * 1024 * 1024)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("big/SKILL.md", "# big\n")
        zf.writestr("big/manifest.json", '{"name":"big","version":"1.0.0","description":"x"}')
        zf.writestr("big/blob.txt", huge_payload)

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    ok, msg = loader.install_skill_from_zip(zip_path)
    assert ok is False
    assert "too large after extraction" in msg


def test_export_skill_to_zip(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    _write_skill(
        workspace,
        "alpha",
        '{"name":"alpha","version":"1.0.0","description":"a","permissions":["read_file"]}',
    )
    out_zip = tmp_path / "out" / "alpha.zip"
    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    ok, msg = loader.export_skill_to_zip("alpha", out_zip)
    assert ok is True
    assert "exported skill: alpha" in msg
    assert "sha256=" in msg
    assert out_zip.exists()

    # Validate export can be re-installed elsewhere.
    ws2 = tmp_path / "ws2"
    ws2.mkdir(parents=True)
    loader2 = SkillsLoader(ws2, builtin_skills_dir=builtin)
    ok2, _ = loader2.install_skill_from_zip(out_zip)
    assert ok2 is True
    assert (ws2 / "skills" / "alpha" / "SKILL.md").exists()


def test_install_skill_dry_run_does_not_write(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    source = tmp_path / "alpha"
    workspace.mkdir(parents=True)
    builtin.mkdir(parents=True)
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("# alpha\n", encoding="utf-8")
    (source / "manifest.json").write_text(
        '{"name":"alpha","version":"1.0.0","description":"a","permissions":["read_file"]}',
        encoding="utf-8",
    )
    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    ok, msg = loader.install_skill_from_dir(source, dry_run=True)
    assert ok is True
    assert "dry-run ok" in msg
    assert not (workspace / "skills" / "alpha").exists()



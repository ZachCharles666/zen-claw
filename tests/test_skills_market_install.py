import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from zen_claw.cli.commands import app
from zen_claw.skills.registry import RegistryEntry


def _skill_zip_bytes(skill_name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{skill_name}/SKILL.md", f"# {skill_name}\n")
        zf.writestr(
            f"{skill_name}/manifest.json",
            json.dumps(
                {
                    "name": skill_name,
                    "version": "1.0.0",
                    "description": "demo",
                    "permissions": ["read_file"],
                }
            ),
        )
    return buf.getvalue()


def _patch_cfg(monkeypatch, workspace: Path, trusted_hosts: list[str] | None = None) -> None:
    cfg = SimpleNamespace(
        workspace_path=workspace,
        skills_market=SimpleNamespace(
            registry_url="https://registry.example.com/index.json",
            cache_file="registry_cache.json",
            cache_ttl_sec=3600,
            trusted_hosts=trusted_hosts or [],
        ),
    )
    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda: cfg)


def test_skills_install_market_download_success(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True)
    _patch_cfg(monkeypatch, workspace, trusted_hosts=["downloads.example.com"])

    row = RegistryEntry(
        name="web-search",
        version="1.0.0",
        description="x",
        download_url="https://downloads.example.com/web-search.zip",
    )
    monkeypatch.setattr("zen_claw.skills.registry.SkillsRegistry.fetch", lambda self, force=False: [row])

    payload = _skill_zip_bytes("web-search")

    class _Resp:
        content = payload

        def raise_for_status(self):
            return None

    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: _Resp())

    out = runner.invoke(app, ["skills", "install", "market:web-search"])
    assert out.exit_code == 0
    assert "installed skill: web-search" in out.output
    assert (workspace / "skills" / "web-search" / "SKILL.md").exists()


def test_skills_install_market_rejects_yanked(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True)
    _patch_cfg(monkeypatch, workspace, trusted_hosts=["downloads.example.com"])

    row = RegistryEntry(
        name="bad-skill",
        version="1.0.0",
        description="x",
        download_url="https://downloads.example.com/bad.zip",
        yanked=True,
    )
    monkeypatch.setattr("zen_claw.skills.registry.SkillsRegistry.fetch", lambda self, force=False: [row])

    out = runner.invoke(app, ["skills", "install", "market:bad-skill"])
    assert out.exit_code == 1
    assert "yanked" in out.output.lower()


def test_skills_install_url_rejects_untrusted_host(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True)
    _patch_cfg(monkeypatch, workspace, trusted_hosts=["trusted.example.com"])

    out = runner.invoke(app, ["skills", "install", "https://evil.example.com/skill.zip"])
    assert out.exit_code == 1
    assert "not in trusted hosts" in out.output.lower()


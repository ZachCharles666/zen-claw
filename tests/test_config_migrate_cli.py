import json
from pathlib import Path

from typer.testing import CliRunner

from zen_claw.cli.commands import app


def test_config_migrate_dry_run_detects_changes(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "tools": {
                    "exec": {"mode": "sidecar", "sidecarUrl": "http://127.0.0.1:4488/v1/exec"},
                }
            }
        ),
        encoding="utf-8",
    )

    out = CliRunner().invoke(app, ["config", "migrate", "--config", str(cfg)])
    assert out.exit_code == 0
    assert "dry-run" in out.output
    assert "Changed paths:" in out.output
    assert "tools.network" in out.output

    payload = json.loads(cfg.read_text(encoding="utf-8"))
    assert "network" not in payload.get("tools", {})


def test_config_migrate_write_persists_migrated_config(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "tools": {
                    "exec": {"mode": "sidecar", "sidecarUrl": "http://127.0.0.1:4488/v1/exec"},
                }
            }
        ),
        encoding="utf-8",
    )

    out = CliRunner().invoke(app, ["config", "migrate", "--config", str(cfg), "--write"])
    assert out.exit_code == 0
    assert "Config migrated successfully" in out.output

    payload = json.loads(cfg.read_text(encoding="utf-8"))
    assert "network" in payload["tools"]
    assert payload["tools"]["network"]["exec"]["mode"] == "sidecar"
    assert cfg.with_suffix(".json.bak").exists()


def test_config_migrate_write_outfile(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    out_cfg = tmp_path / "migrated.json"
    cfg.write_text(
        json.dumps(
            {
                "tools": {
                    "exec": {"mode": "sidecar", "sidecarUrl": "http://127.0.0.1:4488/v1/exec"},
                }
            }
        ),
        encoding="utf-8",
    )

    out = CliRunner().invoke(
        app, ["config", "migrate", "--config", str(cfg), "--write", "--out", str(out_cfg)]
    )
    assert out.exit_code == 0
    assert out_cfg.exists()
    assert not cfg.with_suffix(".json.bak").exists()
    src_payload = json.loads(cfg.read_text(encoding="utf-8"))
    assert "network" not in src_payload.get("tools", {})
    dst_payload = json.loads(out_cfg.read_text(encoding="utf-8"))
    assert "network" in dst_payload.get("tools", {})

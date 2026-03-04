import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from zen_claw.config.loader import convert_keys, get_config_path, load_config
from zen_claw.config.schema import Config


def test_load_config_returns_defaults_when_file_missing(tmp_path: Path) -> None:
    cfg = load_config(config_path=tmp_path / "missing.json")
    assert isinstance(cfg, Config)
    assert isinstance(cfg.agents.defaults.model, str)
    assert cfg.agents.defaults.model != ""


def test_load_config_parses_valid_json_file(tmp_path: Path) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps({"agents": {"defaults": {"model": "openai/gpt-4o", "maxToolIterations": 10}}}),
        encoding="utf-8",
    )
    cfg = load_config(config_path=config_file)
    assert cfg.agents.defaults.model == "openai/gpt-4o"
    assert cfg.agents.defaults.max_tool_iterations == 10


def test_load_config_falls_back_on_malformed_json(tmp_path: Path, capsys) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text("{not-json", encoding="utf-8")
    cfg = load_config(config_path=config_file)
    captured = capsys.readouterr()
    assert isinstance(cfg, Config)
    assert "Warning" in captured.out


def test_config_schema_rejects_invalid_field_type() -> None:
    bad = {"agents": {"defaults": {"maxToolIterations": "not-a-number"}}}
    with pytest.raises((ValidationError, ValueError)):
        Config.model_validate(convert_keys(bad))


def test_get_config_path_under_home_directory() -> None:
    path = get_config_path()
    assert isinstance(path, Path)
    assert path.name == "config.json"
    path.relative_to(Path.home())


def test_load_config_tools_network_browser_mode(tmp_path: Path) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps(
            {
                "tools": {
                    "network": {
                        "browser": {"mode": "sidecar", "sidecarUrl": "http://127.0.0.1:4500/v1/browser"}
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(config_path=config_file)
    assert cfg.tools.network.browser.mode == "sidecar"
    assert "4500" in cfg.tools.network.browser.sidecar_url


def test_load_config_sidecar_supervisor_fields(tmp_path: Path) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps(
            {
                "tools": {
                    "sidecarSupervisorFailWindowSec": 200,
                    "sidecarSupervisorFailThreshold": 8,
                    "sidecarSupervisorCircuitOpenSec": 90,
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(config_path=config_file)
    assert cfg.tools.sidecar_supervisor_fail_window_sec == 200
    assert cfg.tools.sidecar_supervisor_fail_threshold == 8
    assert cfg.tools.sidecar_supervisor_circuit_open_sec == 90

import os
import time
from pathlib import Path

from zen_claw.config.schema import Config
from zen_claw.runtime.sidecar_supervisor import SidecarSupervisor, collect_sidecar_status


def test_collect_sidecar_status_empty_when_no_sidecar_modes(tmp_path: Path) -> None:
    cfg = Config()
    cfg.tools.network.exec.mode = "local"
    cfg.tools.network.search.mode = "local"
    cfg.tools.network.fetch.mode = "local"
    cfg.tools.network.browser.mode = "off"

    rows = collect_sidecar_status(cfg, state_dir=tmp_path)
    assert rows == []


def test_collect_sidecar_status_includes_exec_pid_and_uptime(tmp_path: Path) -> None:
    cfg = Config()
    cfg.tools.network.exec.mode = "sidecar"

    state_file = tmp_path / "sec-execd.json"
    state_file.write_text(
        (
            "{"
            f"\"name\":\"sec-execd\",\"managed\":true,\"pid\":{os.getpid()},"
            f"\"started_at_unix\":{int(time.time()) - 10},\"status\":\"running\""
            "}"
        ),
        encoding="utf-8",
    )

    rows = collect_sidecar_status(cfg, state_dir=tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "sec-execd"
    assert row["pid"] == os.getpid()
    assert row["status"] == "running"
    assert row["uptime"] != "-"


def test_collect_sidecar_status_includes_browser_sidecar(tmp_path: Path) -> None:
    cfg = Config()
    cfg.tools.network.browser.mode = "sidecar"

    state_file = tmp_path / "browser-sidecar.json"
    state_file.write_text(
        (
            "{"
            f"\"name\":\"browser-sidecar\",\"managed\":true,\"pid\":{os.getpid()},"
            f"\"started_at_unix\":{int(time.time()) - 5},\"status\":\"running\""
            "}"
        ),
        encoding="utf-8",
    )

    rows = collect_sidecar_status(cfg, state_dir=tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "browser-sidecar"
    assert row["pid"] == os.getpid()
    assert row["status"] == "running"


def test_collect_sidecar_status_preserves_backoff_or_circuit_state(tmp_path: Path, monkeypatch) -> None:
    cfg = Config()
    cfg.tools.network.exec.mode = "sidecar"

    state_file = tmp_path / "sec-execd.json"
    state_file.write_text(
        "{\"name\":\"sec-execd\",\"managed\":true,\"pid\":null,\"started_at_unix\":null,\"status\":\"circuit_open_42s\"}",
        encoding="utf-8",
    )

    # Isolate from any real sidecar that may be running (e.g. from integration tests).
    monkeypatch.setattr("zen_claw.runtime.sidecar_supervisor._check_health", lambda *a, **kw: False)

    rows = collect_sidecar_status(cfg, state_dir=tmp_path)
    assert len(rows) == 1
    assert rows[0]["status"] == "circuit_open_42s"


def test_sidecar_supervisor_prefers_binary_env_var(tmp_path: Path, monkeypatch) -> None:
    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path / "ws")
    cfg.tools.network.exec.mode = "sidecar"
    sup = SidecarSupervisor(cfg, state_dir=tmp_path / "state")
    spec = sup._specs[0]

    fake_bin = tmp_path / "sec-execd.exe"
    fake_bin.write_text("", encoding="utf-8")
    monkeypatch.setenv("zen_claw_SEC_EXECD_BIN", str(fake_bin))
    cmd = sup._resolve_launch_command(spec)
    assert cmd == [str(fake_bin)]


def test_sidecar_supervisor_falls_back_to_go_run(tmp_path: Path, monkeypatch) -> None:
    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path / "ws")
    cfg.tools.network.exec.mode = "sidecar"
    sup = SidecarSupervisor(cfg, state_dir=tmp_path / "state")
    spec = sup._specs[0]

    monkeypatch.delenv("zen_claw_SEC_EXECD_BIN", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: "go" if name == "go" else None)
    cmd = sup._resolve_launch_command(spec)
    assert cmd == ["go", "run", "."]


def test_sidecar_supervisor_browser_falls_back_to_node_server(tmp_path: Path, monkeypatch) -> None:
    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path / "ws")
    cfg.tools.network.browser.mode = "sidecar"
    sup = SidecarSupervisor(cfg, state_dir=tmp_path / "state")
    spec = [s for s in sup._specs if s.name == "browser-sidecar"][0]

    browser_dir = Path.cwd() / "browser" / "sidecar"
    browser_dir.mkdir(parents=True, exist_ok=True)
    script = browser_dir / "server.js"
    if not script.exists():
        script.write_text("// test stub\n", encoding="utf-8")

    monkeypatch.delenv("zen_claw_BROWSER_SIDECAR_BIN", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: "node" if name == "node" else None)
    cmd = sup._resolve_launch_command(spec)
    assert cmd == ["node", "server.js"]


def test_sidecar_supervisor_passes_browser_state_dir_env(tmp_path: Path, monkeypatch) -> None:
    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path / "ws")
    cfg.tools.network.browser.mode = "sidecar"
    monkeypatch.setenv("BROWSER_SIDECAR_STATE_DIR", str(tmp_path / "browser-state"))
    sup = SidecarSupervisor(cfg, state_dir=tmp_path / "state")
    spec = [s for s in sup._specs if s.name == "browser-sidecar"][0]
    assert spec.env.get("BROWSER_SIDECAR_STATE_DIR") == str(tmp_path / "browser-state")


def test_sidecar_supervisor_records_exponential_backoff(tmp_path: Path, monkeypatch) -> None:
    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path / "ws")
    cfg.tools.network.exec.mode = "sidecar"
    sup = SidecarSupervisor(cfg, state_dir=tmp_path / "state")

    t = 1000.0
    monkeypatch.setattr("time.time", lambda: t)
    sup._record_start_failure("sec-execd")
    first = sup._restart_state["sec-execd"]["next_retry_at_unix"]

    monkeypatch.setattr("time.time", lambda: t + 1)
    sup._record_start_failure("sec-execd")
    second = sup._restart_state["sec-execd"]["next_retry_at_unix"]

    assert first == t + 1
    assert second == (t + 1) + 2


def test_sidecar_supervisor_opens_circuit_after_failure_threshold(tmp_path: Path, monkeypatch) -> None:
    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path / "ws")
    cfg.tools.network.exec.mode = "sidecar"
    cfg.tools.sidecar_supervisor_fail_window_sec = 60
    cfg.tools.sidecar_supervisor_fail_threshold = 3
    cfg.tools.sidecar_supervisor_circuit_open_sec = 90
    sup = SidecarSupervisor(cfg, state_dir=tmp_path / "state")

    monkeypatch.setattr("time.time", lambda: 1000.0)
    sup._record_start_failure("sec-execd")
    sup._record_start_failure("sec-execd")
    assert sup._circuit_open_until.get("sec-execd", 0.0) == 0.0
    sup._record_start_failure("sec-execd")
    assert sup._circuit_open_until.get("sec-execd", 0.0) == 1090.0

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from zen_claw.config.schema import Config
from zen_claw.runtime.sidecar_supervisor import SidecarSupervisor, _ServiceSpec


def _cfg() -> Config:
    cfg = Config()
    cfg.tools.network.browser.mode = "sidecar"
    cfg.tools.sidecar_supervisor_fail_window_sec = 60
    cfg.tools.sidecar_supervisor_fail_threshold = 3
    cfg.tools.sidecar_supervisor_circuit_open_sec = 30
    return cfg


def _supervisor(tmp_path: Path) -> SidecarSupervisor:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return SidecarSupervisor(_cfg(), state_dir=state_dir)


def _dummy_spec() -> _ServiceSpec:
    return _ServiceSpec(
        name="test-sidecar",
        health_url="http://127.0.0.1:19999/healthz",
        bind_address="127.0.0.1:19999",
        cwd=Path("missing-dir"),
        env={},
        binary_env_var="zen_claw_TEST_BIN",
        binary_name="test-bin",
    )


def test_start_failure_recorded_in_failure_history(tmp_path: Path) -> None:
    sup = _supervisor(tmp_path)
    sup._record_start_failure("test-sidecar")
    sup._record_start_failure("test-sidecar")
    hist = sup._failure_history.get("test-sidecar", [])
    assert len(hist) == 2


def test_circuit_opens_after_threshold_failures(tmp_path: Path, monkeypatch) -> None:
    sup = _supervisor(tmp_path)
    monkeypatch.setattr("time.time", lambda: 1000.0)
    sup._record_start_failure("test-sidecar")
    sup._record_start_failure("test-sidecar")
    assert sup._circuit_open_until.get("test-sidecar", 0) == 0
    sup._record_start_failure("test-sidecar")
    assert sup._circuit_open_until.get("test-sidecar", 0) > 1000.0


def test_ensure_running_skips_launch_when_circuit_open(tmp_path: Path) -> None:
    sup = _supervisor(tmp_path)
    spec = _dummy_spec()
    sup._circuit_open_until[spec.name] = time.time() + 60
    with patch("subprocess.Popen", side_effect=AssertionError("Popen should not be called")):
        asyncio.run(sup._ensure_running(spec))


def test_backoff_wait_prevents_relaunch(tmp_path: Path) -> None:
    sup = _supervisor(tmp_path)
    spec = _dummy_spec()
    sup._restart_state[spec.name] = {"next_retry_at_unix": time.time() + 60}
    with patch("subprocess.Popen", side_effect=AssertionError("Popen should not be called")):
        asyncio.run(sup._ensure_running(spec))
    state_file = tmp_path / "state" / f"{spec.name}.json"
    if state_file.exists():
        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert "backoff_wait_" in str(state.get("status") or "")


def test_healthy_external_process_not_restarted(tmp_path: Path) -> None:
    sup = _supervisor(tmp_path)
    spec = _dummy_spec()
    with (
        patch("zen_claw.runtime.sidecar_supervisor._check_health", return_value=True),
        patch("subprocess.Popen", side_effect=AssertionError("Popen should not be called")),
    ):
        asyncio.run(sup._ensure_running(spec))
    state_file = tmp_path / "state" / f"{spec.name}.json"
    assert state_file.exists()
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state.get("status") == "external_healthy"


def test_stop_terminates_procs_and_clears_state(tmp_path: Path) -> None:
    sup = _supervisor(tmp_path)
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None
    sup._procs["browser-sidecar"] = fake_proc
    sup._started_at["browser-sidecar"] = int(time.time()) - 5
    asyncio.run(sup.stop())
    assert sup._procs == {}
    assert sup._started_at == {}

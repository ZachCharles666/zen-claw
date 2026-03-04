import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from zen_claw.config.schema import Config
from zen_claw.dashboard.server import build_dashboard_snapshot, run_dashboard_server


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, Any] | str]:
    req = urllib.request.Request(url, method=method, data=body)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            text = resp.read().decode("utf-8")
            try:
                return int(resp.status), json.loads(text)
            except (ValueError, json.JSONDecodeError):
                return int(resp.status), text
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8")
        try:
            return int(exc.code), json.loads(text)
        except (ValueError, json.JSONDecodeError):
            return int(exc.code), text


def _prepare_data_dir(data_dir: Path) -> None:
    (data_dir / "cron").mkdir(parents=True, exist_ok=True)
    (data_dir / "channels").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes").mkdir(parents=True, exist_ok=True)
    (data_dir / "cron" / "jobs.json").write_text('{"jobs":[]}', encoding="utf-8")
    (data_dir / "channels" / "rate_limit_stats.json").write_text('{"channels":{}}', encoding="utf-8")
    (data_dir / "nodes" / "state.json").write_text(
        '{"version":1,"nodes":{},"tasks":[],"approval_events":[]}',
        encoding="utf-8",
    )


def _start_server(cfg: Config, port: int) -> None:
    thread = threading.Thread(
        target=run_dashboard_server,
        kwargs={"config": cfg, "host": "127.0.0.1", "port": port, "refresh_sec": 1},
        daemon=True,
    )
    thread.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError("dashboard server did not start")


def test_build_dashboard_snapshot_structure(monkeypatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _prepare_data_dir(data_dir)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr("zen_claw.runtime.sidecar_supervisor.collect_sidecar_status", lambda _cfg: [])
    snap = build_dashboard_snapshot(Config())
    assert "agent" in snap
    assert "cron" in snap
    assert "node" in snap
    assert "channels" in snap
    assert "sidecars" in snap


def test_dashboard_healthz(monkeypatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _prepare_data_dir(data_dir)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setenv("zen_claw_DASHBOARD_TOKEN", "")
    port = _free_port()
    _start_server(Config(), port)
    status, body = _request(f"http://127.0.0.1:{port}/healthz")
    assert status == 200
    assert body == "ok"


def test_dashboard_status_endpoint(monkeypatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _prepare_data_dir(data_dir)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setenv("zen_claw_DASHBOARD_TOKEN", "")
    port = _free_port()
    _start_server(Config(), port)
    status, body = _request(f"http://127.0.0.1:{port}/api/status")
    assert status == 200
    assert isinstance(body, dict)
    assert body.get("cron", {}).get("total_jobs") == 0


def test_post_cron_without_token_env_is_backward_compatible(monkeypatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _prepare_data_dir(data_dir)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(
        "zen_claw.dashboard.server.trigger_cron_job_with_audit",
        lambda job_id, *, data_dir: {"ok": True, "job_id": job_id},
    )
    monkeypatch.setenv("zen_claw_DASHBOARD_TOKEN", "")
    port = _free_port()
    _start_server(Config(), port)
    status, body = _request(
        f"http://127.0.0.1:{port}/api/cron/run/job-a",
        method="POST",
        headers={"X-zen-claw-Confirm": "run"},
    )
    assert status == 200
    assert isinstance(body, dict)
    assert body.get("ok") is True


def test_post_cron_with_token_missing_header_returns_401(monkeypatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _prepare_data_dir(data_dir)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(
        "zen_claw.dashboard.server.trigger_cron_job_with_audit",
        lambda job_id, *, data_dir: {"ok": True, "job_id": job_id},
    )
    monkeypatch.setenv("zen_claw_DASHBOARD_TOKEN", "secret-1")
    port = _free_port()
    _start_server(Config(), port)
    status, body = _request(
        f"http://127.0.0.1:{port}/api/cron/run/job-a",
        method="POST",
        headers={"X-zen-claw-Confirm": "run"},
    )
    assert status == 401
    assert isinstance(body, dict)
    assert body.get("error_code") == "dashboard_auth_failed"


def test_post_cron_with_valid_token_returns_non_401(monkeypatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _prepare_data_dir(data_dir)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(
        "zen_claw.dashboard.server.trigger_cron_job_with_audit",
        lambda job_id, *, data_dir: {"ok": True, "job_id": job_id},
    )
    monkeypatch.setenv("zen_claw_DASHBOARD_TOKEN", "secret-1")
    port = _free_port()
    _start_server(Config(), port)
    status, body = _request(
        f"http://127.0.0.1:{port}/api/cron/run/job-a",
        method="POST",
        headers={"X-zen-claw-Confirm": "run", "X-zen-claw-Token": "secret-1"},
    )
    assert status == 200
    assert isinstance(body, dict)
    assert body.get("ok") is True


def teardown_module() -> None:
    os.environ.pop("zen_claw_DASHBOARD_TOKEN", None)

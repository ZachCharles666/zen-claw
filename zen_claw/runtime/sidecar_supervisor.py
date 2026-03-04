"""Local supervisor for sec-execd and net-proxy sidecars."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

from loguru import logger


@dataclass
class _ServiceSpec:
    name: str
    health_url: str
    bind_address: str
    cwd: Path
    env: dict[str, str]
    binary_env_var: str
    binary_name: str


def _healthz_from_url(url: str, suffix: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or ""
    if path.endswith(suffix):
        path = path[: -len(suffix)] + "/healthz"
    else:
        path = "/healthz"
    return parsed._replace(path=path, params="", query="", fragment="").geturl()


def _bind_from_url(url: str, default_port: int) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or default_port
    return f"{host}:{port}"


def _is_pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_pipe(pipe, sidecar_name: str, is_stderr: bool = False) -> None:
    """Drain a subprocess pipe and forward each line to loguru.

    JSON lines are emitted as-is (audit events from Go sidecars).
    Non-JSON lines fall back to debug/warning level.
    The function is designed to run in a daemon thread and exits cleanly on EOF.
    """
    try:
        for raw in pipe:
            line = raw.rstrip()
            if not line:
                continue
            try:
                json.loads(line)  # validate it's a structured audit event
                logger.info("sidecar {} | {}", sidecar_name, line)
            except json.JSONDecodeError:
                if is_stderr:
                    logger.warning("sidecar {} stderr | {}", sidecar_name, line)
                else:
                    logger.debug("sidecar {} stdout | {}", sidecar_name, line)
    except Exception:  # pipe closed or process gone
        pass


def _check_health(health_url: str, timeout_sec: float = 0.7) -> bool:
    try:
        with urlopen(health_url, timeout=timeout_sec) as resp:
            return int(resp.status) < 400
    except (URLError, TimeoutError, ValueError):
        return False


def _format_uptime(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours:02d}:{mins:02d}:{secs:02d}"
    return f"{hours:02d}:{mins:02d}:{secs:02d}"


def _build_service_specs(config, workspace: Path) -> list[_ServiceSpec]:
    specs: list[_ServiceSpec] = []
    exec_cfg = config.tools.effective_exec()
    search_cfg = config.tools.effective_search()
    fetch_cfg = config.tools.effective_fetch()
    browser_cfg = config.tools.effective_browser()

    if exec_cfg.mode == "sidecar":
        env = {
            "SEC_EXECD_BIND": _bind_from_url(exec_cfg.sidecar_url, 4488),
            "SEC_EXECD_WORKSPACE": str(workspace),
            "SEC_EXECD_REQUIRE_APPROVAL": "true",
        }
        if exec_cfg.sidecar_approval_mode == "hmac":
            env["SEC_EXECD_APPROVAL_SECRET"] = exec_cfg.sidecar_approval_token.get_secret_value()
            env["SEC_EXECD_APPROVAL_TOKEN"] = ""
        else:
            env["SEC_EXECD_APPROVAL_TOKEN"] = exec_cfg.sidecar_approval_token.get_secret_value()
            env["SEC_EXECD_APPROVAL_SECRET"] = ""
        specs.append(
            _ServiceSpec(
                name="sec-execd",
                health_url=_healthz_from_url(exec_cfg.sidecar_url, "/v1/exec"),
                bind_address=env["SEC_EXECD_BIND"],
                cwd=Path("go/sec-execd"),
                env=env,
                binary_env_var="zen_claw_SEC_EXECD_BIN",
                binary_name="sec-execd",
            )
        )

    proxy_urls: list[str] = []
    if search_cfg.mode == "proxy":
        proxy_urls.append(search_cfg.proxy_url)
    if fetch_cfg.mode == "proxy":
        proxy_urls.append(fetch_cfg.proxy_url)

    if proxy_urls:
        first = proxy_urls[0]
        first_bind = _bind_from_url(first, 4499)
        mismatch = any(_bind_from_url(url, 4499) != first_bind for url in proxy_urls[1:])
        if mismatch:
            logger.warning(
                "sidecar supervisor skipped net-proxy auto-start because proxy URLs use different bind addresses"
            )
        else:
            specs.append(
                _ServiceSpec(
                    name="net-proxy",
                    health_url=_healthz_from_url(first, "/v1/search"),
                    bind_address=first_bind,
                    cwd=Path("go/net-proxy"),
                    env={
                        "NET_PROXY_BIND": first_bind,
                    },
                    binary_env_var="zen_claw_NET_PROXY_BIN",
                    binary_name="net-proxy",
                )
            )
    if browser_cfg.mode == "sidecar":
        bind = _bind_from_url(browser_cfg.sidecar_url, 4500)
        env = {
            "BROWSER_SIDECAR_BIND": bind,
            "BROWSER_SIDECAR_MAX_STEPS": str(browser_cfg.max_steps),
            "BROWSER_SIDECAR_TIMEOUT_SEC": str(browser_cfg.timeout_sec),
        }
        if browser_cfg.allowed_domains:
            env["BROWSER_SIDECAR_ALLOW_DOMAINS"] = ",".join(browser_cfg.allowed_domains)
        if browser_cfg.blocked_domains:
            env["BROWSER_SIDECAR_DENY_DOMAINS"] = ",".join(browser_cfg.blocked_domains)
        state_dir = os.environ.get("BROWSER_SIDECAR_STATE_DIR", "").strip()
        if state_dir:
            env["BROWSER_SIDECAR_STATE_DIR"] = state_dir
        specs.append(
            _ServiceSpec(
                name="browser-sidecar",
                health_url=_healthz_from_url(browser_cfg.sidecar_url, "/v1/browser"),
                bind_address=bind,
                cwd=Path("browser/sidecar"),
                env=env,
                binary_env_var="zen_claw_BROWSER_SIDECAR_BIN",
                binary_name="browser-sidecar",
            )
        )
    return specs


def collect_sidecar_status(config, *, state_dir: Path | None = None) -> list[dict[str, Any]]:
    """Collect sidecar runtime status from state files + health checks."""
    from zen_claw.config.loader import get_data_dir

    workspace = Path(config.workspace_path)
    base = state_dir or (get_data_dir() / "sidecars")
    specs = _build_service_specs(config, workspace)

    now = int(time.time())
    rows: list[dict[str, Any]] = []
    for spec in specs:
        state_file = base / f"{spec.name}.json"
        pid = None
        started_at = None
        managed = False
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
                pid = int(state.get("pid") or 0) or None
                started_at = int(state.get("started_at_unix") or 0) or None
                managed = bool(state.get("managed", True))
                state_status = str(state.get("status") or "").strip()
            except (ValueError, OSError, json.JSONDecodeError):
                state_status = ""
        else:
            state_status = ""

        pid_alive = _is_pid_alive(pid)
        healthy = _check_health(spec.health_url)
        uptime = "-"
        if started_at and pid_alive:
            uptime = _format_uptime(now - started_at)

        if pid_alive:
            status = "running"
        elif healthy:
            status = "external_healthy"
        elif state_status and state_status != "stopped":
            status = state_status
        else:
            status = "stopped"

        rows.append(
            {
                "name": spec.name,
                "status": status,
                "managed": managed,
                "pid": pid if pid_alive else None,
                "uptime": uptime,
                "health": healthy,
                "bind": spec.bind_address,
                "health_url": spec.health_url,
            }
        )
    return rows


class SidecarSupervisor:
    """Supervise local sidecar processes with health-based restart."""

    def __init__(
        self,
        config,
        *,
        state_dir: Path | None = None,
        go_executable: str = "go",
        monitor_interval_sec: float = 5.0,
    ):
        from zen_claw.config.loader import get_data_dir

        self.config = config
        self.workspace = Path(config.workspace_path)
        self.state_dir = state_dir or (get_data_dir() / "sidecars")
        self.go_executable = go_executable
        self.monitor_interval_sec = monitor_interval_sec
        self._specs = _build_service_specs(config, self.workspace)
        self._fail_window_sec = max(1, int(getattr(config.tools, "sidecar_supervisor_fail_window_sec", 120)))
        self._fail_threshold = max(1, int(getattr(config.tools, "sidecar_supervisor_fail_threshold", 5)))
        self._circuit_open_sec = max(1, int(getattr(config.tools, "sidecar_supervisor_circuit_open_sec", 120)))
        self._procs: dict[str, subprocess.Popen[str]] = {}
        self._started_at: dict[str, int] = {}
        self._monitor_task: asyncio.Task | None = None
        self._restart_state: dict[str, dict[str, float]] = {}
        self._failure_history: dict[str, list[float]] = {}
        self._circuit_open_until: dict[str, float] = {}

    async def start(self) -> None:
        if not self._specs:
            return
        self.state_dir.mkdir(parents=True, exist_ok=True)
        for spec in self._specs:
            await self._ensure_running(spec)
        if self._monitor_task is None:
            self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def stop(self) -> None:
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None

        for name, proc in list(self._procs.items()):
            self._terminate_process(proc)
            self._write_state(name, managed=True, pid=None, started_at_unix=None, status="stopped")
        self._procs.clear()
        self._started_at.clear()

    async def _monitor_loop(self) -> None:
        while True:
            await asyncio.sleep(self.monitor_interval_sec)
            for spec in self._specs:
                await self._ensure_running(spec)

    async def _ensure_running(self, spec: _ServiceSpec) -> None:
        now = time.time()
        circuit_until = float(self._circuit_open_until.get(spec.name, 0.0) or 0.0)
        if circuit_until > now:
            self._write_state(
                spec.name,
                managed=True,
                pid=None,
                started_at_unix=None,
                status=f"circuit_open_{int(circuit_until - now)}s",
            )
            return

        state = self._restart_state.get(spec.name, {})
        next_retry_at = float(state.get("next_retry_at_unix", 0.0) or 0.0)
        if next_retry_at > now:
            self._write_state(
                spec.name,
                managed=True,
                pid=None,
                started_at_unix=None,
                status=f"backoff_wait_{int(next_retry_at - now)}s",
            )
            return

        proc = self._procs.get(spec.name)
        if proc is not None and proc.poll() is None and _check_health(spec.health_url):
            self._write_state(
                spec.name,
                managed=True,
                pid=proc.pid,
                started_at_unix=self._started_at.get(spec.name),
                status="running",
            )
            return
        if _check_health(spec.health_url):
            self._write_state(spec.name, managed=False, pid=None, started_at_unix=None, status="external_healthy")
            return

        if proc is not None and proc.poll() is None:
            self._terminate_process(proc)

        launch_cmd = self._resolve_launch_command(spec)
        if launch_cmd is None:
            self._record_start_failure(spec.name)
            self._write_state(spec.name, managed=True, pid=None, started_at_unix=None, status="launcher_not_found")
            logger.warning(f"sidecar supervisor cannot start {spec.name}: launcher not found")
            return

        cwd = Path.cwd() / spec.cwd
        if not cwd.exists():
            self._record_start_failure(spec.name)
            self._write_state(spec.name, managed=True, pid=None, started_at_unix=None, status="binary_path_missing")
            logger.warning(f"sidecar supervisor cannot start {spec.name}: missing path {cwd}")
            return

        env = os.environ.copy()
        env.update(spec.env)
        proc_new = subprocess.Popen(
            launch_cmd,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered so audit events are forwarded promptly
        )
        for _pipe, _is_err in ((proc_new.stdout, False), (proc_new.stderr, True)):
            if _pipe is not None:
                threading.Thread(
                    target=_read_pipe,
                    args=(_pipe, spec.name, _is_err),
                    daemon=True,
                    name=f"pipe-{spec.name}-{'err' if _is_err else 'out'}",
                ).start()
        self._procs[spec.name] = proc_new
        self._started_at[spec.name] = int(time.time())

        ok = False
        for _ in range(20):
            await asyncio.sleep(0.25)
            if proc_new.poll() is not None:
                break
            if _check_health(spec.health_url):
                ok = True
                break

        if ok:
            self._restart_state[spec.name] = {}
            self._failure_history[spec.name] = []
            self._circuit_open_until[spec.name] = 0.0
            self._write_state(
                spec.name,
                managed=True,
                pid=proc_new.pid,
                started_at_unix=self._started_at[spec.name],
                status="running",
            )
            logger.info(f"sidecar supervisor started {spec.name} pid={proc_new.pid} bind={spec.bind_address}")
            return

        self._terminate_process(proc_new)
        self._record_start_failure(spec.name)
        self._write_state(spec.name, managed=True, pid=None, started_at_unix=None, status="start_failed")
        logger.warning(f"sidecar supervisor failed to start {spec.name}")

    def _state_path(self, name: str) -> Path:
        return self.state_dir / f"{name}.json"

    def _write_state(
        self,
        name: str,
        *,
        managed: bool,
        pid: int | None,
        started_at_unix: int | None,
        status: str,
    ) -> None:
        payload = {
            "name": name,
            "managed": managed,
            "pid": pid,
            "started_at_unix": started_at_unix,
            "status": status,
            "updated_at_unix": int(time.time()),
        }
        try:
            self._state_path(name).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            logger.warning(f"failed to write sidecar state file for {name}")

    @staticmethod
    def _terminate_process(proc: subprocess.Popen[str]) -> None:
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                proc.send_signal(signal.SIGKILL)
                proc.wait(timeout=1)
            except Exception:
                proc.kill()
                proc.wait(timeout=1)
        except Exception:
            proc.kill()
            proc.wait(timeout=1)

    def _resolve_launch_command(self, spec: _ServiceSpec) -> list[str] | None:
        env_path = os.environ.get(spec.binary_env_var, "").strip()
        if env_path and Path(env_path).exists():
            return [env_path]

        exe_suffix = ".exe" if os.name == "nt" else ""
        binary_filename = f"{spec.binary_name}{exe_suffix}"
        candidates = [
            Path.cwd() / "bin" / binary_filename,
            Path.cwd() / spec.cwd / binary_filename,
        ]
        for candidate in candidates:
            if candidate.exists():
                return [str(candidate)]

        if spec.name == "browser-sidecar":
            node_bin = shutil.which("node")
            script = Path.cwd() / spec.cwd / "server.js"
            if node_bin and script.exists():
                return [node_bin, "server.js"]

        go_bin = shutil.which(self.go_executable)
        if go_bin:
            return [go_bin, "run", "."]
        return None

    def _record_start_failure(self, name: str) -> None:
        now = time.time()
        state = self._restart_state.get(name, {})
        attempts = int(state.get("attempts", 0.0) or 0) + 1
        delay = min(30.0, float(2 ** min(attempts - 1, 5)))
        history = self._failure_history.get(name, [])
        history.append(now)
        history = [ts for ts in history if now - ts <= self._fail_window_sec]
        self._failure_history[name] = history
        if len(history) >= self._fail_threshold:
            self._circuit_open_until[name] = now + self._circuit_open_sec
            logger.warning(
                "sidecar supervisor circuit opened "
                + f"name={name} failures={len(history)} window={self._fail_window_sec}s"
            )
        self._restart_state[name] = {
            "attempts": float(attempts),
            "next_retry_at_unix": now + delay,
            "last_fail_at_unix": now,
        }

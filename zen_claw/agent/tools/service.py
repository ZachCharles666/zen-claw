"""Simple workspace-local background service manager (pidfile based)."""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
from pathlib import Path
from typing import Any

from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult


def _services_dir(workspace: Path) -> Path:
    p = workspace / ".services"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _pid_file(workspace: Path, name: str) -> Path:
    return _services_dir(workspace) / f"{name}.pid"


def _log_file(workspace: Path, name: str) -> Path:
    return _services_dir(workspace) / f"{name}.log"


def _is_process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return False
            exit_code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return bool(ok) and int(exit_code.value) == STILL_ACTIVE
        os.kill(pid, 0)
        return True
    except Exception:
        return False


class ServiceStartTool(Tool):
    name = "service_start"
    description = "Start a background service process and save pidfile."
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "command": {"type": "string"},
            "cwd": {"type": "string"},
        },
        "required": ["name", "command"],
    }

    def __init__(self, workspace: Path):
        self._workspace = Path(workspace).resolve()

    async def execute(self, name: str, command: str, cwd: str | None = None, **kwargs: Any) -> ToolResult:
        name = str(name or "").strip()
        cmd = str(command or "").strip()
        if not name or not cmd:
            return ToolResult.failure(ToolErrorKind.PARAMETER, "name and command are required", code="service_missing_args")
        pid_path = _pid_file(self._workspace, name)
        if pid_path.exists():
            try:
                pid = int(pid_path.read_text().strip())
            except Exception:
                pid = -1
            if _is_process_running(pid):
                return ToolResult.failure(
                    ToolErrorKind.PERMISSION, f"service '{name}' already running", code="service_already_running"
                )
            pid_path.unlink(missing_ok=True)
        workdir = self._workspace if not cwd else (self._workspace / cwd).resolve()
        try:
            workdir.relative_to(self._workspace)
        except Exception:
            return ToolResult.failure(ToolErrorKind.PERMISSION, "cwd must stay inside workspace", code="service_cwd_outside_workspace")
        workdir.mkdir(parents=True, exist_ok=True)
        log_path = _log_file(self._workspace, name)
        try:
            log_f = open(log_path, "a", encoding="utf-8")
            if os.name == "nt":
                proc = subprocess.Popen(
                    cmd,
                    shell=False,
                    cwd=str(workdir),
                    stdout=log_f,
                    stderr=log_f,
                    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                argv = shlex.split(cmd, posix=True)
                proc = subprocess.Popen(
                    argv, shell=False, cwd=str(workdir), stdout=log_f, stderr=log_f, start_new_session=True
                )
        except Exception as exc:
            return ToolResult.failure(ToolErrorKind.RUNTIME, f"failed to start service: {exc}", code="service_start_failed")
        pid_path.write_text(str(proc.pid), encoding="utf-8")
        return ToolResult.success(
            json.dumps(
                {"name": name, "pid": proc.pid, "status": "started", "pid_file": str(pid_path), "log_file": str(log_path)},
                ensure_ascii=False,
            )
        )


class ServiceStopTool(Tool):
    name = "service_stop"
    description = "Stop a background service process by pidfile."
    parameters = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "force_kill": {"type": "boolean"}},
        "required": ["name"],
    }

    def __init__(self, workspace: Path):
        self._workspace = Path(workspace).resolve()

    async def execute(self, name: str, force_kill: bool = False, **kwargs: Any) -> ToolResult:
        name = str(name or "").strip()
        pid_path = _pid_file(self._workspace, name)
        if not pid_path.exists():
            return ToolResult.failure(ToolErrorKind.PARAMETER, f"service '{name}' not found", code="service_not_found")
        try:
            pid = int(pid_path.read_text().strip())
        except Exception:
            pid_path.unlink(missing_ok=True)
            return ToolResult.failure(ToolErrorKind.RUNTIME, "corrupted pidfile", code="service_pid_corrupt")
        if not _is_process_running(pid):
            pid_path.unlink(missing_ok=True)
            return ToolResult.success(json.dumps({"name": name, "pid": pid, "status": "already_stopped"}))
        try:
            if os.name == "nt":
                args = ["taskkill", "/PID", str(pid)]
                if force_kill:
                    args.append("/F")
                subprocess.run(args, check=False, capture_output=True)
            else:
                os.kill(pid, signal.SIGKILL if force_kill else signal.SIGTERM)
        except Exception as exc:
            return ToolResult.failure(ToolErrorKind.RUNTIME, f"failed to stop service: {exc}", code="service_stop_failed")
        pid_path.unlink(missing_ok=True)
        return ToolResult.success(json.dumps({"name": name, "pid": pid, "status": "stopped"}))


class ServiceStatusTool(Tool):
    name = "service_status"
    description = "Check one service or list all services."
    parameters = {"type": "object", "properties": {"name": {"type": "string"}}, "required": []}

    def __init__(self, workspace: Path):
        self._workspace = Path(workspace).resolve()

    async def execute(self, name: str | None = None, **kwargs: Any) -> ToolResult:
        if name:
            return self._status_one(str(name))
        rows = []
        for p in sorted(_services_dir(self._workspace).glob("*.pid")):
            svc = p.stem
            try:
                pid = int(p.read_text().strip())
            except Exception:
                pid = -1
            rows.append({"name": svc, "pid": pid, "running": _is_process_running(pid)})
        return ToolResult.success(json.dumps({"services": rows}, ensure_ascii=False))

    def _status_one(self, name: str) -> ToolResult:
        pid_path = _pid_file(self._workspace, name)
        log_path = _log_file(self._workspace, name)
        if not pid_path.exists():
            return ToolResult.success(json.dumps({"name": name, "running": False, "pid": None}))
        try:
            pid = int(pid_path.read_text().strip())
        except Exception:
            return ToolResult.success(json.dumps({"name": name, "running": False, "pid": None, "error": "corrupt_pidfile"}))
        return ToolResult.success(
            json.dumps(
                {
                    "name": name,
                    "pid": pid,
                    "running": _is_process_running(pid),
                    "pid_file": str(pid_path),
                    "log_file": str(log_path) if log_path.exists() else None,
                },
                ensure_ascii=False,
            )
        )

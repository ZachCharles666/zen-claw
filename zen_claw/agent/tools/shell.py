"""Shell execution tool."""

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult
from zen_claw.agent.tools.sidecar_approval import build_hmac_approval_headers, hmac_body_json


class ExecTool(Tool):
    """Tool to execute shell commands."""

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
        mode: str = "local",
        sidecar_url: str = "http://127.0.0.1:4488/v1/exec",
        sidecar_approval_mode: str = "token",
        sidecar_approval_token: str = "",
        sidecar_fallback_to_local: bool = False,
        sidecar_healthcheck: bool = False,
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self.deny_patterns = deny_patterns or [
            r"\brm\s+-[rf]{1,2}\b",          # rm -r, rm -rf, rm -fr
            r"\bdel\s+/[fq]\b",              # del /f, del /q
            r"\brmdir\s+/s\b",               # rmdir /s
            r"\b(format|mkfs|diskpart)\b",   # disk operations
            r"\bdd\s+if=",                   # dd
            r">\s*/dev/sd",                  # write to disk
            r"\b(shutdown|reboot|poweroff)\b",  # system power
            r":\(\)\s*\{.*\};\s*:",          # fork bomb
        ]
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
        self.mode = mode
        self.sidecar_url = sidecar_url
        self.sidecar_approval_mode = sidecar_approval_mode
        self.sidecar_approval_token = sidecar_approval_token
        self.sidecar_fallback_to_local = sidecar_fallback_to_local
        self.sidecar_healthcheck = sidecar_healthcheck

    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return "Execute a shell command and return its output. Use with caution."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute"
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command"
                }
            },
            "required": ["command"]
        }

    async def execute(self, command: str, working_dir: str | None = None, env: dict[str, str] | None = None, **kwargs: Any) -> ToolResult:
        cwd = working_dir or self.working_dir or os.getcwd()
        trace_id = str(kwargs.get("trace_id") or "")
        guard = self._guard_command(command, cwd)
        if guard:
            code, message = guard
            return ToolResult.failure(ToolErrorKind.PERMISSION, message, code=code)

        if self.mode == "sidecar":
            return await self._execute_via_sidecar(command=command, cwd=cwd, env=env, trace_id=trace_id)

        return await self._execute_local(command=command, cwd=cwd, env=env)

    async def _execute_local(self, command: str, cwd: str, env: dict[str, str] | None = None) -> ToolResult:
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()  # reap the process to avoid zombie accumulation
                return ToolResult.failure(
                    ToolErrorKind.RETRYABLE,
                    f"Command timed out after {self.timeout} seconds",
                    code="exec_timeout",
                )

            output_parts = []

            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))

            if stderr:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if stderr_text.strip():
                    output_parts.append(f"STDERR:\n{stderr_text}")

            if process.returncode != 0:
                output_parts.append(f"\nExit code: {process.returncode}")

            result = "\n".join(output_parts) if output_parts else "(no output)"

            # Truncate very long output
            max_len = 10000
            if len(result) > max_len:
                result = result[:max_len] + f"\n... (truncated, {len(result) - max_len} more chars)"

            if process.returncode != 0:
                return ToolResult.failure(
                    ToolErrorKind.RUNTIME,
                    f"Command exited with code {process.returncode}",
                    code="exec_nonzero_exit",
                    content=result,
                )

            return ToolResult.success(result)

        except Exception as e:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                f"Error executing command: {str(e)}",
                code="exec_failed",
            )

    async def _execute_via_sidecar(self, command: str, cwd: str, env: dict[str, str] | None = None, trace_id: str = "") -> ToolResult:
        payload = {
            "command": command,
            "working_dir": cwd,
            "timeout_seconds": self.timeout,
        }
        if env is not None:
            payload["env"] = env
        body_bytes = hmac_body_json(payload)
        headers = {"Content-Type": "application/json"}
        if self.sidecar_approval_mode == "hmac":
            if not trace_id:
                return ToolResult.failure(
                    ToolErrorKind.PARAMETER,
                    "trace_id is required for sidecar_approval_mode=hmac",
                    code="exec_sidecar_trace_required",
                )
            if not self.sidecar_approval_token:
                return ToolResult.failure(
                    ToolErrorKind.PARAMETER,
                    "sidecar_approval_token must be set as HMAC secret for hmac mode",
                    code="exec_sidecar_secret_missing",
                )
            headers.update(
                build_hmac_approval_headers(
                    secret=self.sidecar_approval_token,
                    trace_id=trace_id,
                    method="POST",
                    path="/v1/exec",
                    body_bytes=body_bytes,
                )
            )
        else:
            if self.sidecar_approval_token:
                headers["X-Approval-Token"] = self.sidecar_approval_token
            if trace_id:
                headers["X-Trace-Id"] = trace_id

        try:
            async with httpx.AsyncClient(timeout=self.timeout + 5) as client:
                if self.sidecar_healthcheck:
                    health = await client.get(self._healthz_url())
                    if health.status_code >= 400:
                        if self.sidecar_fallback_to_local:
                            return await self._execute_local(command=command, cwd=cwd, env=env)
                        return ToolResult.failure(
                            ToolErrorKind.RETRYABLE,
                            f"Sidecar health check failed with HTTP {health.status_code}",
                            code="exec_sidecar_unhealthy",
                        )
                if self.sidecar_approval_mode == "hmac":
                    response = await client.post(self.sidecar_url, headers=headers, content=body_bytes)
                else:
                    response = await client.post(self.sidecar_url, headers=headers, json=payload)
        except httpx.TimeoutException as e:
            if self.sidecar_fallback_to_local:
                return await self._execute_local(command=command, cwd=cwd, env=env)
            return ToolResult.failure(
                ToolErrorKind.RETRYABLE,
                f"Sidecar request timed out: {str(e)}",
                code="exec_sidecar_timeout",
            )
        except httpx.RequestError as e:
            if self.sidecar_fallback_to_local:
                return await self._execute_local(command=command, cwd=cwd, env=env)
            return ToolResult.failure(
                ToolErrorKind.RETRYABLE,
                f"Sidecar request failed: {str(e)}",
                code="exec_sidecar_unreachable",
            )

        try:
            data = response.json()
        except json.JSONDecodeError:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                "Sidecar returned invalid JSON",
                code="exec_sidecar_invalid_response",
            )

        if response.status_code >= 400:
            err_msg = str(data.get("error_message") or f"HTTP {response.status_code}")
            err_code = str(data.get("error_code") or "exec_sidecar_http_error")
            kind = ToolErrorKind.PERMISSION if response.status_code in (401, 403) else ToolErrorKind.RUNTIME
            return ToolResult.failure(kind, err_msg, code=err_code, http_status=response.status_code)

        ok = bool(data.get("ok"))
        stdout = str(data.get("stdout") or "")
        stderr = str(data.get("stderr") or "")
        exit_code = int(data.get("exit_code") or 0)

        content_parts = []
        if stdout:
            content_parts.append(stdout)
        if stderr:
            content_parts.append(f"STDERR:\n{stderr}")
        if not content_parts:
            content_parts.append("(no output)")
        content = "\n".join(content_parts)

        if ok:
            return ToolResult.success(
                content,
                exit_code=exit_code,
                sidecar=True,
            )

        err_msg = str(data.get("error_message") or f"Command exited with code {exit_code}")
        err_code = str(data.get("error_code") or "exec_sidecar_failed")
        if err_code in {"approval_required", "dangerous_command", "working_dir_outside_workspace"}:
            kind = ToolErrorKind.PERMISSION
        elif err_code in {"command_timeout"}:
            kind = ToolErrorKind.RETRYABLE
        else:
            kind = ToolErrorKind.RUNTIME

        return ToolResult.failure(
            kind,
            err_msg,
            code=err_code,
            content=content,
            exit_code=exit_code,
            sidecar=True,
        )

    def _healthz_url(self) -> str:
        parsed = urlparse(self.sidecar_url)
        path = parsed.path
        if path.endswith("/v1/exec"):
            path = path[: -len("/v1/exec")] + "/healthz"
        else:
            path = "/healthz"
        return parsed._replace(path=path, params="", query="", fragment="").geturl()

    def _guard_command(self, command: str, cwd: str) -> tuple[str, str] | None:
        """Best-effort safety guard for potentially destructive commands."""
        cmd = command.strip()
        lower = cmd.lower()

        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                return ("exec_guard_dangerous_pattern", "Command blocked by safety guard (dangerous pattern detected)")

        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return ("exec_guard_not_allowlisted", "Command blocked by safety guard (not in allowlist)")

        if self.restrict_to_workspace:
            # Double URL-decode to catch %2e%2e/ and double-encoded %252e%252e/ variants.
            # Note: authoritative path enforcement should also occur at the sidecar/
            # filesystem layer; this string-level check is a best-effort pre-execution guard.
            cmd_decoded = unquote(unquote(cmd)).lower()
            if any(p in cmd_decoded for p in ("../", "..\\" , "\x00")):
                return ("exec_path_traversal", "Command blocked by safety guard (path traversal detected)")

            cwd_path = Path(cwd).resolve()

            win_paths = re.findall(r"[A-Za-z]:\\[^\\\"']+", cmd)
            posix_paths = re.findall(r"/[^\s\"']+", cmd)

            for raw in win_paths + posix_paths:
                try:
                    p = Path(raw).resolve()
                except Exception:
                    continue
                if cwd_path not in p.parents and p != cwd_path:
                    return ("exec_path_outside_working_dir", "Command blocked by safety guard (path outside working dir)")

        return None



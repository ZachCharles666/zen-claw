"""Gateway tool proxy for isolating untrusted skill execution."""

import os
from pathlib import Path
from typing import Any

from loguru import logger

from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult
from zen_claw.agent.tools.shell import ExecTool


class GatewayToolLocalStub(Tool):
    """Stub registered in local mode so skills get a clear error instead of silent failure (LOW-010)."""

    @property
    def name(self) -> str:
        return "gateway"

    @property
    def description(self) -> str:
        return "Execute an isolated command in a secure sandbox (unavailable in local mode)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command to execute in the sandbox."},
                "working_dir": {"type": "string", "description": "Optional working directory."},
            },
            "required": ["command"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult.failure(
            ToolErrorKind.PERMISSION,
            "Gateway tool requires sidecar mode. Start zen-claw with exec.mode=sidecar to enable sandboxed execution.",
            code="gateway_requires_sidecar",
        )


class GatewayTool(Tool):
    """
    Acts as an isolation boundary for untrusted skills.
    
    Instead of executing tools locally (which might allow an untrusted skill
    to bypass restrictions if not carefully bounded), this gateway spawns
    a fresh sub-process or routes to the sidecar (sec-execd) depending on config.
    """

    def __init__(self, backend_tool: ExecTool, allowed_commands: list[str] | None = None, workspace: str | None = None):
        """
        Initialize the gateway.

        Args:
            backend_tool: An ExecTool instance configured for sidecar execution.
            allowed_commands: Optional allowlist of base commands (e.g., ["python", "node"]).
            workspace: The root workspace directory to base the sandbox on.
        """
        self.backend = backend_tool
        self.allowed_commands = allowed_commands or []
        self.workspace = workspace
        self._name = "gateway"
        self._description = "Execute an isolated command in a secure sandbox. Used for untrusted skill execution."

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command to execute in the sandbox."},
                "working_dir": {"type": "string", "description": "Optional working directory."},
            },
            "required": ["command"],
        }

    async def execute(
        self,
        command: str,
        working_dir: str | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        """Execute a command securely through the backend proxy."""
        base_cmd = command.strip().split()[0] if command.strip() else ""
        if self.allowed_commands and base_cmd not in self.allowed_commands:
            logger.warning(f"Gateway rejected command: {base_cmd} not in allowlist {self.allowed_commands}")
            return ToolResult.failure(
                ToolErrorKind.PERMISSION,
                f"Command '{base_cmd}' is not allowed in this sandbox.",
                code="gateway_command_rejected",
            )

        logger.info(f"Gateway routing isolated command: {command}")

        # Directory isolation
        sandbox_dir = (Path(self.workspace) / ".sandbox").resolve() if self.workspace else Path(os.getcwd()) / ".sandbox"
        sandbox_dir.mkdir(parents=True, exist_ok=True)

        target_dir = Path(working_dir).resolve() if working_dir else sandbox_dir
        if sandbox_dir not in target_dir.parents and target_dir != sandbox_dir:
            return ToolResult.failure(
                ToolErrorKind.PERMISSION,
                f"Working directory '{target_dir}' is outside the sandbox '{sandbox_dir}'",
                code="gateway_path_traversal",
            )

        # Environment sanitization
        allowed_env_keys = {
            "PATH",
            "LANG",
            "OS",
            "SYSTEMDRIVE",
            "SYSTEMROOT",
            "TMP",
            "TEMP",
            "COMSPEC",
            "WINDIR",
            "PATHEXT",
        }
        sanitized_env = {
            k: v for k, v in os.environ.items() if str(k).strip().upper() in allowed_env_keys
        }

        # Strip potentially confusing kwargs before passing to exec backend
        clean_kwargs = {k: v for k, v in kwargs.items() if k in ["trace_id"]}

        result = await self.backend.execute(
            command=command,
            working_dir=str(target_dir),
            env=sanitized_env,
            **clean_kwargs,
        )
        if self._should_fallback_builtin(command, result):
            return self._fallback_builtin(command, sanitized_env)
        return result

    @staticmethod
    def _should_fallback_builtin(command: str, result: ToolResult) -> bool:
        if result.ok:
            return False
        if not result.error or result.error.code != "exec_failed":
            return False
        msg = str(result.error.message or "")
        if "winerror 5" not in msg.lower():
            return False
        token = (command or "").strip().lower()
        return token == "set" or token.startswith("echo ")

    @staticmethod
    def _fallback_builtin(command: str, env: dict[str, str]) -> ToolResult:
        text = (command or "").strip()
        lower = text.lower()
        if lower == "set":
            lines = [f"{k}={v}" for k, v in sorted(env.items(), key=lambda item: item[0].lower())]
            return ToolResult.success("\n".join(lines) if lines else "(no output)")
        if lower.startswith("echo "):
            return ToolResult.success(text[5:].lstrip())
        return ToolResult.failure(
            ToolErrorKind.RUNTIME,
            "Gateway builtin fallback did not match command",
            code="gateway_builtin_fallback_miss",
        )

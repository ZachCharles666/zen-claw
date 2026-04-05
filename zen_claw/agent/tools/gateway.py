"""Gateway tool proxy for isolating untrusted skill execution."""
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
                "command": {
                    "type": "string",
                    "description": "The command to execute in the sandbox.",
                },
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
    Acts as a strict sidecar-only isolation boundary for untrusted skills.
    """

    def __init__(
        self,
        backend_tool: ExecTool,
        allowed_commands: list[str] | None = None,
    ):
        """
        Initialize the gateway.

        Args:
            backend_tool: An ExecTool instance configured for sidecar execution.
            allowed_commands: Optional allowlist of base commands (e.g., ["python", "node"]).
        """
        self.backend = backend_tool
        self.allowed_commands = allowed_commands or []
        self._name = "gateway"
        self._description = (
            "Execute an isolated command in a secure sandbox. Used for untrusted skill execution."
        )

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
                "command": {
                    "type": "string",
                    "description": "The command to execute in the sandbox.",
                },
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
            logger.warning(
                f"Gateway rejected command: {base_cmd} not in allowlist {self.allowed_commands}"
            )
            return ToolResult.failure(
                ToolErrorKind.PERMISSION,
                f"Command '{base_cmd}' is not allowed in this sandbox.",
                code="gateway_command_rejected",
            )

        logger.info(f"Gateway routing isolated command: {command}")

        # Strip potentially confusing kwargs before passing to exec backend
        clean_kwargs = {k: v for k, v in kwargs.items() if k in ["trace_id"]}

        return await self.backend.execute(
            command=command,
            working_dir=str(working_dir or ""),
            **clean_kwargs,
        )

"""Agent identity tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult
from zen_claw.auth.identity import AgentIdentity, AgentIdentityError


def _get_identity(workspace: Path, key_dir_override: str | None = None) -> AgentIdentity:
    key_dir = Path(key_dir_override).expanduser() if key_dir_override else workspace / ".agent_keys"
    identity = AgentIdentity(key_dir)
    identity.get_or_create_keypair()
    return identity


class AgentSignTool(Tool):
    name = "agent_sign"
    description = "Sign a message with this agent ed25519 private key."
    parameters = {
        "type": "object",
        "properties": {"message": {"type": "string", "minLength": 1}},
        "required": ["message"],
    }

    def __init__(self, workspace: Path, key_dir_override: str | None = None):
        self._workspace = workspace
        self._key_dir_override = key_dir_override

    async def execute(self, message: str, **kwargs: Any) -> ToolResult:
        if not str(message or "").strip():
            return ToolResult.failure(
                ToolErrorKind.PARAMETER, "message must not be empty", code="sign_empty_message"
            )
        try:
            identity = _get_identity(self._workspace, self._key_dir_override)
            sig = identity.sign(str(message).encode("utf-8"))
            pub = identity.public_key_hex()
        except AgentIdentityError as exc:
            return ToolResult.failure(ToolErrorKind.RUNTIME, str(exc), code="identity_error")
        except Exception as exc:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME, f"Signing failed: {exc}", code="sign_failed"
            )
        return ToolResult.success(
            json.dumps(
                {"message": message, "signature": sig, "public_key": pub, "algorithm": "ed25519"},
                ensure_ascii=False,
            )
        )


class AgentPublicKeyTool(Tool):
    name = "agent_public_key"
    description = "Get this agent ed25519 public key and metadata."
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, workspace: Path, key_dir_override: str | None = None):
        self._workspace = workspace
        self._key_dir_override = key_dir_override

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            identity = _get_identity(self._workspace, self._key_dir_override)
            pub = identity.public_key_hex()
            created_at = identity.created_at()
        except AgentIdentityError as exc:
            return ToolResult.failure(ToolErrorKind.RUNTIME, str(exc), code="identity_error")
        except Exception as exc:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME, f"Failed to retrieve public key: {exc}", code="pubkey_failed"
            )
        return ToolResult.success(
            json.dumps(
                {"public_key": pub, "algorithm": "ed25519", "created_at": created_at},
                ensure_ascii=False,
            )
        )


class AgentVerifyTool(Tool):
    name = "agent_verify"
    description = "Verify an ed25519 signature."
    parameters = {
        "type": "object",
        "properties": {
            "public_key": {"type": "string", "description": "Hex string of the public key"},
            "message": {"type": "string", "description": "The original signed message data"},
            "signature": {"type": "string", "description": "Base64 urlsafe signature"},
        },
        "required": ["public_key", "message", "signature"],
    }

    def __init__(self, workspace: Path, key_dir_override: str | None = None):
        self._workspace = workspace
        self._key_dir_override = key_dir_override

    async def execute(
        self, public_key: str, message: str, signature: str, **kwargs: Any
    ) -> ToolResult:
        try:
            valid = AgentIdentity.verify(public_key, str(message).encode("utf-8"), signature)
            return ToolResult.success(json.dumps({"valid": valid}, ensure_ascii=False))
        except Exception as exc:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME, f"Verification error: {exc}", code="verify_failed"
            )

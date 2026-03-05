"""Credential vault tools for agent runtime."""

from __future__ import annotations

from typing import Any

from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult
from zen_claw.auth.credentials import CredentialVault


class CredentialStoreTool(Tool):
    name = "credential_store"
    description = "Store a credential in encrypted vault."
    parameters = {
        "type": "object",
        "properties": {
            "platform": {"type": "string", "description": "Platform name, e.g. github/twitter"},
            "key": {"type": "string", "description": "Credential key name"},
            "value": {"type": "string", "description": "Credential plaintext value"},
        },
        "required": ["platform", "key", "value"],
    }

    def __init__(self, vault: CredentialVault | None = None):
        self._vault = vault or CredentialVault()

    async def execute(self, platform: str, key: str, value: str, **kwargs: Any) -> ToolResult:
        platform = str(platform or "").strip().lower()
        key = str(key or "").strip()
        if not platform:
            return ToolResult.failure(
                ToolErrorKind.PARAMETER,
                "platform must be non-empty",
                code="credential_invalid_platform",
            )
        if not key:
            return ToolResult.failure(
                ToolErrorKind.PARAMETER,
                "key must be non-empty",
                code="credential_invalid_key",
            )
        if not isinstance(value, str):
            return ToolResult.failure(
                ToolErrorKind.PARAMETER,
                "value must be a string",
                code="credential_invalid_value",
            )
        try:
            self._vault.store(platform, key, value)
        except Exception as exc:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                f"failed to store credential: {exc}",
                code="credential_store_error",
            )
        return ToolResult.success(f"Credential stored: platform={platform!r}, key={key!r}")


class CredentialGetTool(Tool):
    name = "credential_get"
    description = "Get a decrypted credential value from vault."
    parameters = {
        "type": "object",
        "properties": {
            "platform": {"type": "string", "description": "Platform name used when storing"},
            "key": {"type": "string", "description": "Credential key name"},
        },
        "required": ["platform", "key"],
    }

    def __init__(self, vault: CredentialVault | None = None):
        self._vault = vault or CredentialVault()

    async def execute(self, platform: str, key: str, **kwargs: Any) -> ToolResult:
        platform = str(platform or "").strip().lower()
        key = str(key or "").strip()
        if not platform or not key:
            return ToolResult.failure(
                ToolErrorKind.PARAMETER,
                "platform and key are required",
                code="credential_missing_args",
            )
        try:
            value = self._vault.get(platform, key)
        except Exception as exc:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                f"failed to read credential: {exc}",
                code="credential_get_error",
            )
        if value is None:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                f"credential not found: platform={platform!r}, key={key!r}",
                code="credential_not_found",
            )
        return ToolResult.success(value)

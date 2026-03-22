"""API key management with RBAC roles — A3."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any, Literal

# Role hierarchy: admin > operator > readonly
ApiKeyRole = Literal["admin", "operator", "readonly"]

ROLE_WEIGHTS: dict[str, int] = {
    "admin": 3,
    "operator": 2,
    "readonly": 1,
}

# Endpoints that require at least operator level
_OPERATOR_WRITE_PREFIXES = (
    "/api/v1/skills/",
    "/api/v1/agents/",
    "/api/v1/knowledge/",
    "/api/v1/cron/",
    "/api/v1/rag/",
    "/api/v1/token-usage/",
)

# Endpoints that require admin level
_ADMIN_PREFIXES = (
    "/api/v1/admin/",
)


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str]:
    """Return (raw_key, prefix). Prefix is first 12 chars for display."""
    raw = "nc-" + uuid.uuid4().hex + uuid.uuid4().hex[:8]
    return raw, raw[:12]


class ApiKeyStore:
    """
    Manages API keys with RBAC roles stored in a JSON file.

    Schema per entry (keyed by sha256 hash):
        {
          "prefix": "nc-xxxxxxxxxxxx",
          "role": "admin" | "operator" | "readonly",
          "created_at": 1700000000,
          "enabled": true,
          "label": "optional description"
        }
    """

    def __init__(self, data_dir: Path) -> None:
        self._path = Path(data_dir) / "api_keys.json"

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def _save(self, keys: dict[str, dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(keys, indent=2, ensure_ascii=False), encoding="utf-8")

    def create(self, role: ApiKeyRole = "readonly", label: str = "") -> tuple[str, str]:
        """
        Create a new API key with the given role.

        Returns (raw_key, prefix).
        """
        raw, prefix = generate_api_key()
        hashed = _hash_key(raw)
        keys = self._load()
        keys[hashed] = {
            "prefix": prefix,
            "role": role,
            "created_at": int(time.time()),
            "enabled": True,
            "label": label,
        }
        self._save(keys)
        return raw, prefix

    def verify(self, raw: str) -> bool:
        """Return True if the key exists and is enabled."""
        hashed = _hash_key(raw)
        entry = self._load().get(hashed)
        return bool(entry and entry.get("enabled", True))

    def get_role(self, raw: str) -> ApiKeyRole | None:
        """Return the role for this key, or None if invalid/disabled."""
        hashed = _hash_key(raw)
        entry = self._load().get(hashed)
        if not entry or not entry.get("enabled", True):
            return None
        role = str(entry.get("role", "readonly"))
        if role not in ROLE_WEIGHTS:
            return "readonly"
        return role  # type: ignore[return-value]

    def has_role(self, raw: str, required_role: ApiKeyRole) -> bool:
        """Return True if key's role is >= required_role in the hierarchy."""
        role = self.get_role(raw)
        if role is None:
            return False
        return ROLE_WEIGHTS.get(role, 0) >= ROLE_WEIGHTS.get(required_role, 0)

    def revoke(self, prefix: str) -> bool:
        """Disable a key by prefix. Returns True if found."""
        keys = self._load()
        found = False
        for entry in keys.values():
            if str(entry.get("prefix", "")).startswith(prefix):
                entry["enabled"] = False
                found = True
        if found:
            self._save(keys)
        return found

    def list_keys(self) -> list[dict[str, Any]]:
        """Return all keys as a list (without the hash, without raw value)."""
        return [
            {
                "prefix": entry.get("prefix", ""),
                "role": entry.get("role", "readonly"),
                "label": entry.get("label", ""),
                "created_at": entry.get("created_at", 0),
                "enabled": entry.get("enabled", True),
            }
            for entry in self._load().values()
        ]


def is_admin_path(path: str) -> bool:
    """Return True if the path requires admin role."""
    return any(path.startswith(p) for p in _ADMIN_PREFIXES)


def is_write_path(method: str, path: str) -> bool:
    """Return True if the request is a state-modifying operation."""
    return method.upper() in {"POST", "PUT", "PATCH", "DELETE"}

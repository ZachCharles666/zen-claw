"""Tenant-scoped path helpers."""

from __future__ import annotations

import re
from pathlib import Path

DEFAULT_TENANT_ID = "default"


def _validate_tenant_id(tenant_id: str) -> None:
    if not tenant_id:
        raise ValueError("tenant_id cannot be empty")
    if tenant_id == DEFAULT_TENANT_ID:
        return
    if not re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", tenant_id):
        raise ValueError(f"tenant_id must be a UUID or 'default', got: {tenant_id!r}")


def tenant_data_dir(base_data_dir: Path, tenant_id: str, sub: str = "") -> Path:
    _validate_tenant_id(tenant_id)
    tenant_root = base_data_dir / "tenants" / tenant_id
    if not sub:
        return tenant_root
    sub_path = Path(sub)
    if sub_path.is_absolute():
        raise ValueError("sub must be relative")
    resolved = (tenant_root / sub_path).resolve()
    if not str(resolved).startswith(str(tenant_root.resolve())):
        raise ValueError(f"Path traversal detected in sub: {sub!r}")
    return resolved


def assert_no_path_crossing(resolved: Path, base: Path) -> None:
    try:
        resolved.relative_to(base.resolve())
    except ValueError as exc:
        raise ValueError(f"Path '{resolved}' is outside allowed base '{base}'") from exc

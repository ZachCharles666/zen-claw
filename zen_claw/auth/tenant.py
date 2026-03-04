"""Tenant store."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from loguru import logger

from zen_claw.auth.paths import _validate_tenant_id


@dataclass
class Tenant:
    tenant_id: str
    name: str
    created_at: float
    quota_llm_calls_per_day: int = 1000
    quota_storage_mb: int = 1000
    enabled: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Tenant":
        return cls(
            tenant_id=str(d["tenant_id"]),
            name=str(d["name"]),
            created_at=float(d.get("created_at", 0)),
            quota_llm_calls_per_day=int(d.get("quota_llm_calls_per_day", 1000)),
            quota_storage_mb=int(d.get("quota_storage_mb", 1000)),
            enabled=bool(d.get("enabled", True)),
        )


class TenantStore:
    def __init__(self, base_data_dir: Path) -> None:
        self.base_data_dir = Path(base_data_dir)
        self._tenants_root = self.base_data_dir / "tenants"

    def _tenant_file(self, tenant_id: str) -> Path:
        _validate_tenant_id(tenant_id)
        return self._tenants_root / tenant_id / "tenant.json"

    def create(self, name: str, quota_llm_calls_per_day: int = 1000, quota_storage_mb: int = 1000) -> Tenant:
        if not name or not name.strip():
            raise ValueError("Tenant name cannot be empty")
        tenant = Tenant(
            tenant_id=str(uuid.uuid4()),
            name=name.strip(),
            created_at=time.time(),
            quota_llm_calls_per_day=quota_llm_calls_per_day,
            quota_storage_mb=quota_storage_mb,
        )
        self._write(tenant)
        logger.info(f"Created tenant {tenant.tenant_id}")
        return tenant

    def get(self, tenant_id: str) -> Optional[Tenant]:
        f = self._tenant_file(tenant_id)
        if not f.exists():
            return None
        try:
            return Tenant.from_dict(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            return None

    def list(self) -> list[Tenant]:
        rows: list[Tenant] = []
        if not self._tenants_root.exists():
            return rows
        for d in self._tenants_root.iterdir():
            if not d.is_dir():
                continue
            f = d / "tenant.json"
            if not f.exists():
                continue
            try:
                rows.append(Tenant.from_dict(json.loads(f.read_text(encoding="utf-8"))))
            except Exception:
                continue
        return sorted(rows, key=lambda x: x.created_at)

    def update(self, tenant: Tenant) -> bool:
        if not self._tenant_file(tenant.tenant_id).exists():
            return False
        self._write(tenant)
        return True

    def delete(self, tenant_id: str) -> bool:
        tenant = self.get(tenant_id)
        if not tenant:
            return False
        tenant.enabled = False
        self._write(tenant)
        return True

    def _write(self, tenant: Tenant) -> None:
        f = self._tenant_file(tenant.tenant_id)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(tenant.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

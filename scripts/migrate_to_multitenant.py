"""Migration script: move single-tenant data to tenants/default/."""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

SKIP_DIRS = {"tenants"}


def migrate(data_dir: Path, dry_run: bool = False) -> None:
    target = data_dir / "tenants" / "default"
    print(f"Migration: {data_dir} -> {target}")
    if dry_run:
        print("DRY RUN - no files will be moved")

    tenant_file = target / "tenant.json"
    if not tenant_file.exists():
        tenant_data = {
            "tenant_id": "default",
            "name": "Default Tenant",
            "created_at": time.time(),
            "quota_llm_calls_per_day": 10000,
            "quota_storage_mb": 10000,
            "enabled": True,
        }
        if not dry_run:
            target.mkdir(parents=True, exist_ok=True)
            tenant_file.write_text(json.dumps(tenant_data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  Created {tenant_file}")

    moved: list[str] = []
    for item in data_dir.iterdir():
        if item.name in SKIP_DIRS or item.name.startswith("."):
            continue
        dest = target / item.name
        if dest.exists():
            print(f"  SKIP (already exists): {item.name}")
            continue
        print(f"  Move: {item} -> {dest}")
        if not dry_run:
            shutil.move(str(item), str(dest))
        moved.append(item.name)
    print(f"\nMigration complete. Moved: {moved}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, help="nano-claw data directory")
    parser.add_argument("--dry-run", action="store_true", help="preview only")
    args = parser.parse_args()
    migrate(Path(args.data_dir).expanduser(), dry_run=args.dry_run)

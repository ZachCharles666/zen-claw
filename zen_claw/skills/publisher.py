"""Skills publisher: validate, package and catalog entry generation."""

from __future__ import annotations

import hashlib
import json
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zen_claw.agent.skills import SkillsLoader

_REQUIRED_MANIFEST_KEYS = {"name", "version", "description", "author", "entry", "permissions"}


@dataclass
class PublishResult:
    ok: bool
    skill_name: str
    zip_path: str = ""
    catalog_entry_path: str = ""
    sha256: str = ""
    error: str = ""


class SkillsPublisher:
    def __init__(
        self, workspace: Path, output_dir: Path | None = None, require_integrity: bool = True
    ):
        self._workspace = Path(workspace).expanduser().resolve()
        self._output_dir = (output_dir or (self._workspace / "dist")).resolve()
        self._require_integrity = require_integrity

    def publish(self, skill_name: str) -> PublishResult:
        skill_dir = self._workspace / "skills" / skill_name
        if not skill_dir.is_dir():
            return PublishResult(
                ok=False, skill_name=skill_name, error=f"Skill directory not found: {skill_dir}"
            )
        manifest_path = skill_dir / "manifest.json"
        try:
            manifest = self._load_manifest(manifest_path)
        except (FileNotFoundError, ValueError) as exc:
            return PublishResult(ok=False, skill_name=skill_name, error=str(exc))
        validator = SkillsLoader(self._workspace)
        valid_manifest, manifest_errors = validator._validate_manifest_file(
            manifest_path,
            skill_name,
            strict=True,
        )
        if not valid_manifest:
            return PublishResult(
                ok=False,
                skill_name=skill_name,
                error="Manifest validation failed: " + "; ".join(manifest_errors),
            )
        if self._require_integrity:
            ok, err = self._check_integrity(skill_dir)
            if not ok:
                return PublishResult(
                    ok=False, skill_name=skill_name, error=f"Integrity check failed: {err}."
                )
        version = str(manifest.get("version", "0.0.0"))
        zip_name = f"{skill_name}-{version}.zip"
        self._output_dir.mkdir(parents=True, exist_ok=True)
        zip_path = self._output_dir / zip_name
        try:
            self._build_zip(skill_dir, zip_path)
        except Exception as exc:
            return PublishResult(
                ok=False, skill_name=skill_name, error=f"Failed to build zip: {exc}"
            )
        sha = self._sha256_file(zip_path)
        catalog = self._make_catalog_entry(manifest, zip_path.name, sha)
        catalog_path = self._output_dir / f"{skill_name}-{version}.catalog.json"
        catalog_path.write_text(json.dumps(catalog, indent=2, ensure_ascii=False), encoding="utf-8")
        return PublishResult(
            ok=True,
            skill_name=skill_name,
            zip_path=str(zip_path),
            catalog_entry_path=str(catalog_path),
            sha256=sha,
        )

    def _load_manifest(self, manifest_path: Path) -> dict[str, Any]:
        if not manifest_path.exists():
            raise FileNotFoundError(f"manifest.json not found at {manifest_path}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in manifest.json: {exc}") from exc
        missing = _REQUIRED_MANIFEST_KEYS - set(manifest.keys())
        if missing:
            raise ValueError(f"manifest.json is missing required keys: {sorted(missing)}.")
        return manifest

    def _check_integrity(self, skill_dir: Path) -> tuple[bool, str]:
        integrity_path = skill_dir / "integrity.json"
        if not integrity_path.exists():
            return False, "integrity.json not found"
        try:
            payload = json.loads(integrity_path.read_text(encoding="utf-8"))
            files_record = payload.get("files", {})
        except Exception as exc:
            return False, f"invalid integrity.json: {exc}"
        if not isinstance(files_record, dict):
            return False, "integrity.json missing files map"
        for rel, expected in files_record.items():
            p = (skill_dir / rel).resolve()
            try:
                p.relative_to(skill_dir.resolve())
            except ValueError:
                return False, f"path outside skill dir: {rel}"
            if not p.exists() or not p.is_file():
                return False, f"file missing: {rel}"
            actual = hashlib.sha256(p.read_bytes()).hexdigest()
            if actual != str(expected):
                return False, f"Hash mismatch: {rel}"
        return True, ""

    def _build_zip(self, skill_dir: Path, out_zip: Path) -> None:
        with zipfile.ZipFile(out_zip, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(skill_dir.rglob("*")):
                if not p.is_file() or p.is_symlink():
                    continue
                rel = p.relative_to(skill_dir)
                arcname = str(rel).replace(os.sep, "/")
                zf.write(p, arcname=arcname)

    @staticmethod
    def _sha256_file(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _make_catalog_entry(
        manifest: dict[str, Any], zip_filename: str, sha256_hex: str
    ) -> dict[str, Any]:
        catalog = {
            "name": str(manifest.get("name", "")),
            "version": str(manifest.get("version", "")),
            "description": str(manifest.get("description", "")),
            "author": str(manifest.get("author", "")),
            "homepage": str(manifest.get("homepage", "")),
            "download_url": f"https://example.com/{zip_filename}",
            "sha256": sha256_hex,
            "tags": list(manifest.get("tags", []) or []),
            "permissions": list(manifest.get("permissions", []) or []),
            "enforce_ready": bool(manifest.get("enforce_ready", False)),
            "size_bytes": 0,
        }
        runtime_contract = manifest.get("runtime_contract")
        if isinstance(runtime_contract, dict):
            catalog["runtime_contract"] = runtime_contract
        return catalog

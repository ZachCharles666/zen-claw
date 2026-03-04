"""Skills market registry: fetch, cache and search."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_REGISTRY_URL = "https://zen-claw.github.io/skills-registry/index.json"


@dataclass
class RegistryEntry:
    name: str
    version: str
    description: str
    author: str = ""
    homepage: str = ""
    download_url: str = ""
    sha256: str = ""
    tags: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    enforce_ready: bool = False
    size_bytes: int = 0
    yanked: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RegistryEntry":
        return cls(
            name=str(d.get("name", "")),
            version=str(d.get("version", "")),
            description=str(d.get("description", "")),
            author=str(d.get("author", "")),
            homepage=str(d.get("homepage", "")),
            download_url=str(d.get("download_url", "")),
            sha256=str(d.get("sha256", "")),
            tags=list(d.get("tags") or []),
            permissions=list(d.get("permissions") or []),
            enforce_ready=bool(d.get("enforce_ready", False)),
            size_bytes=int(d.get("size_bytes", 0)),
            yanked=bool(d.get("yanked", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "homepage": self.homepage,
            "download_url": self.download_url,
            "sha256": self.sha256,
            "tags": self.tags,
            "permissions": self.permissions,
            "enforce_ready": self.enforce_ready,
            "size_bytes": self.size_bytes,
            "yanked": self.yanked,
        }


class SkillsRegistry:
    def __init__(
        self,
        registry_url: str = _DEFAULT_REGISTRY_URL,
        cache_path: Path | None = None,
        cache_ttl_sec: int = 3600,
        http_timeout: float = 15.0,
    ):
        self._url = registry_url
        self._cache_path = cache_path
        self._ttl = cache_ttl_sec
        self._http_timeout = http_timeout
        self._entries: list[RegistryEntry] | None = None

    def fetch(self, force: bool = False) -> list[RegistryEntry]:
        if not force and self._entries is not None:
            return self._entries
        if not force and self._is_cache_valid():
            try:
                self._entries = self._load_cache()
                return self._entries
            except Exception:
                pass
        raw = self._fetch_raw()
        self._entries = self._parse_catalog(raw)
        self._save_cache(raw)
        return self._entries

    def search(
        self,
        query: str = "",
        tag: str = "",
        author: str = "",
        enforce_ready: bool | None = None,
        force_refresh: bool = False,
    ) -> list[RegistryEntry]:
        rows = self.fetch(force=force_refresh)
        out = rows
        if query:
            q = query.lower()
            out = [x for x in out if q in x.name.lower() or q in x.description.lower()]
        if tag:
            t = tag.lower()
            out = [x for x in out if t in [i.lower() for i in x.tags]]
        if author:
            a = author.lower()
            out = [x for x in out if x.author.lower() == a]
        if enforce_ready is not None:
            out = [x for x in out if x.enforce_ready == enforce_ready]
        return sorted(out, key=lambda x: x.name.lower())

    def _is_cache_valid(self) -> bool:
        if self._ttl == 0 or not self._cache_path or not self._cache_path.exists():
            return False
        return (time.time() - self._cache_path.stat().st_mtime) < self._ttl

    def _load_cache(self) -> list[RegistryEntry]:
        if not self._cache_path or not self._cache_path.exists():
            return []
        raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
        return self._parse_catalog(raw)

    def _save_cache(self, raw: dict) -> None:
        if not self._cache_path:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._cache_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._cache_path)

    def _fetch_raw(self) -> dict:
        if self._url.startswith("file://"):
            path = Path(self._url[len("file://") :])
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise RuntimeError(f"Failed to read local catalog at {path}: {exc}") from exc
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError("httpx is required to fetch remote registry") from exc
        try:
            resp = httpx.get(self._url, timeout=self._http_timeout, follow_redirects=True)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            raise RuntimeError(f"Failed to fetch registry from {self._url}: {exc}") from exc

    @staticmethod
    def _parse_catalog(raw: dict) -> list[RegistryEntry]:
        out: list[RegistryEntry] = []
        for item in raw.get("skills", []):
            if not isinstance(item, dict):
                continue
            entry = RegistryEntry.from_dict(item)
            if not entry.name or not entry.version:
                continue
            out.append(entry)
        return out

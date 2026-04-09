"""Skill source adapters for multi-source registry support.

Supported source types:
  registry — any HTTPS URL serving a zen-claw index.json catalog
  github   — a GitHub repository (or subdirectory) containing skill directories
  local    — a local filesystem directory containing skill directories
"""

from __future__ import annotations

import io
import json
import os
import urllib.request
import zipfile
from typing import Protocol, runtime_checkable

from zen_claw.skills.registry import RegistryEntry


@runtime_checkable
class SkillSourceAdapter(Protocol):
    @property
    def source_name(self) -> str:
        ...

    def list_entries(self) -> list[RegistryEntry]:
        ...


class GitHubSkillSource:
    """Discover and fetch skills from a GitHub repository.

    URL formats accepted:
      github:owner/repo
      github:owner/repo/subpath
      https://github.com/owner/repo
      https://github.com/owner/repo/tree/main/subpath
    """

    def __init__(self, url: str, source_name: str = "github") -> None:
        self._source_name = source_name
        self._owner, self._repo, self._path = self._parse_url(url)

    @property
    def source_name(self) -> str:
        return self._source_name

    @staticmethod
    def _parse_url(url: str) -> tuple[str, str, str]:
        """Return (owner, repo, subpath)."""
        if url.startswith("github:"):
            rest = url[len("github:"):]
        elif "github.com/" in url:
            rest = url.split("github.com/", 1)[1]
            # Strip /tree/branch/ prefix if present
            parts = rest.strip("/").split("/")
            if len(parts) >= 4 and parts[2] == "tree":
                rest = "/".join([parts[0], parts[1]] + parts[4:])
        else:
            rest = url
        parts = rest.strip("/").split("/", 2)
        owner = parts[0] if len(parts) > 0 else ""
        repo = parts[1] if len(parts) > 1 else ""
        path = parts[2] if len(parts) > 2 else ""
        return owner, repo, path

    def _api_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "zen-claw/1.0",
        }
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"token {token}"
        return headers

    def _api_get(self, url: str) -> object:
        req = urllib.request.Request(url, headers=self._api_headers())
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            return json.loads(resp.read())

    def _raw_get(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": "zen-claw/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            return resp.read()

    def list_entries(self) -> list[RegistryEntry]:
        """Browse the GitHub repo and return one RegistryEntry per valid skill directory."""
        api_base = f"https://api.github.com/repos/{self._owner}/{self._repo}/contents"
        search_url = f"{api_base}/{self._path}" if self._path else api_base

        try:
            contents = self._api_get(search_url)
        except Exception:
            return []

        if not isinstance(contents, list):
            return []

        entries: list[RegistryEntry] = []
        for item in contents:
            if item.get("type") != "dir":
                continue
            dir_path = item["path"]
            dir_name = item["name"]

            try:
                dir_contents = self._api_get(f"{api_base}/{dir_path}")
            except Exception:
                continue
            if not isinstance(dir_contents, list):
                continue

            files = {f["name"]: f for f in dir_contents if f.get("type") == "file"}
            if "SKILL.md" not in files or "manifest.json" not in files:
                continue

            try:
                manifest_raw = self._raw_get(files["manifest.json"]["download_url"])
                manifest = json.loads(manifest_raw)
            except Exception:
                continue
            if not isinstance(manifest, dict):
                continue

            entries.append(
                RegistryEntry(
                    name=manifest.get("name", dir_name),
                    version=manifest.get("version", "0.0.0"),
                    description=manifest.get("description", ""),
                    author=manifest.get("author", f"{self._owner}/{self._repo}"),
                    permissions=manifest.get("permissions", []),
                    tags=manifest.get("tags", []),
                    download_url="",  # not used; fetch_skill_zip() provides bytes
                    sha256="",
                )
            )
        return entries

    def fetch_skill_zip(self, skill_name: str) -> bytes | None:
        """Download all files for one skill directory and return as an in-memory zip.

        The zip contains a single top-level directory named *skill_name* so that
        the existing install_and_sanitize_skill_from_zip() pipeline accepts it.
        """
        api_base = f"https://api.github.com/repos/{self._owner}/{self._repo}/contents"
        skill_path = f"{self._path}/{skill_name}" if self._path else skill_name

        try:
            dir_contents = self._api_get(f"{api_base}/{skill_path}")
        except Exception:
            return None
        if not isinstance(dir_contents, list):
            return None

        files = {f["name"]: f for f in dir_contents if f.get("type") == "file"}
        if "SKILL.md" not in files:
            return None

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for fname, finfo in files.items():
                try:
                    content = self._raw_get(finfo["download_url"])
                    zf.writestr(f"{skill_name}/{fname}", content)
                except Exception:
                    continue
        return buf.getvalue()


class LocalDirSkillSource:
    """Discover skills from a local filesystem directory.

    Scans *root_dir* for subdirectories that contain both SKILL.md and
    manifest.json and returns them as RegistryEntry objects.
    """

    def __init__(self, root_dir: str, source_name: str = "local") -> None:
        self._source_name = source_name
        self._root = root_dir

    @property
    def source_name(self) -> str:
        return self._source_name

    def list_entries(self) -> list[RegistryEntry]:
        from pathlib import Path

        root = Path(self._root).expanduser().resolve()
        if not root.is_dir():
            return []
        entries: list[RegistryEntry] = []
        for candidate in root.iterdir():
            if not candidate.is_dir():
                continue
            skill_md = candidate / "SKILL.md"
            manifest_path = candidate / "manifest.json"
            if not skill_md.exists():
                continue
            manifest: dict = {}
            if manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            entries.append(
                RegistryEntry(
                    name=manifest.get("name", candidate.name),
                    version=manifest.get("version", "0.0.0"),
                    description=manifest.get("description", ""),
                    author=manifest.get("author", "local"),
                    permissions=manifest.get("permissions", []),
                    tags=manifest.get("tags", []),
                    download_url=str(candidate),
                    sha256="",
                )
            )
        return entries

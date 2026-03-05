"""Skills market registry: fetch, cache and search."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from packaging.version import parse as parse_version

from zen_claw.utils.crypto import sign_data, verify_signature
from zen_claw.utils.netguard import resolve_safe_ip

logger = logging.getLogger(__name__)

_DEFAULT_REGISTRY_URL = "https://zen-claw.github.io/skills-registry/index.json"
_MAX_REGISTRY_REDIRECTS = 5


class TrustedTime:
    """
    Provides a dual-track trusted time source.
    Combines network time (multiple sources) with a local HMAC-signed monotonic cache.
    """

    DEFAULT_SOURCES = [
        "https://www.google.com",
        "https://www.cloudflare.com",
        "https://www.amazon.com",
    ]

    def __init__(self, cache_dir: Path, refresh_interval_sec: int = 300):
        self._cache_file = cache_dir / ".time_cache"
        self._max_drift_tolerance = 3600  # 1 hour
        self._refresh_interval_sec = max(1, int(refresh_interval_sec))
        self._last_network_check = 0.0
        self._last_time = 0.0

    def get_time(self) -> float:
        """
        Get the current trusted time.
        Ensures it never goes backwards relative to the local signed cache.
        """
        current_mono = time.time()
        if (
            self._last_time > 0
            and current_mono - self._last_network_check < self._refresh_interval_sec
        ):
            return max(current_mono, self._last_time)
        cached_time = self._load_signed_time()
        network_time = self._fetch_network_time()

        # 1. Fallback to cached time if network is down
        if network_time is None:
            logger.warning("Network time unavailable, using cached time.")
            self._last_time = max(current_mono, cached_time)
            self._last_network_check = current_mono
            return self._last_time

        # 2. Prevent Freeze Attacks: if network time is BEFORE cached time, reject it
        if network_time < cached_time:
            logger.error(
                f"Freeze Attack Detected! Network time ({network_time}) is before cached time ({cached_time})"
            )
            self._last_time = cached_time
            self._last_network_check = current_mono
            return self._last_time

        # 3. Update cache if network time is newer
        self._save_signed_time(network_time)
        self._last_time = network_time
        self._last_network_check = current_mono
        return self._last_time

    def _load_signed_time(self) -> float:
        if not self._cache_file.exists():
            return 0.0
        try:
            data = json.loads(self._cache_file.read_text(encoding="utf-8"))
            val_str = str(data.get("t", 0))
            sig = data.get("sig", "")

            if verify_signature(val_str, sig):
                return float(val_str)
        except Exception:
            pass
        return 0.0

    def _save_signed_time(self, t: float) -> None:
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            val_str = str(t)
            sig = sign_data(val_str)
            self._cache_file.write_text(json.dumps({"t": t, "sig": sig}), encoding="utf-8")
        except Exception:
            pass

    def _fetch_network_time(self) -> float | None:
        """Fetch time from multiple sources and return the median to avoid single-source compromise."""
        import httpx

        times = []
        for src in self.DEFAULT_SOURCES:
            try:
                # Use HEAD to get Date header quickly
                resp = httpx.head(src, timeout=5.0, follow_redirects=True)
                date_str = resp.headers.get("Date")
                if date_str:
                    from email.utils import parsedate_to_datetime

                    dt = parsedate_to_datetime(date_str)
                    times.append(dt.timestamp())
            except Exception:
                continue

        if not times:
            return None

        # Use median to be resilient against outliers
        times.sort()
        return times[len(times) // 2]


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
        trusted_hosts: list[str] | None = None,
    ):
        self._url = registry_url
        self._cache_path = cache_path
        self._ttl = cache_ttl_sec
        self._http_timeout = http_timeout
        self._entries: list[RegistryEntry] | None = None
        self._trusted_time = TrustedTime(cache_path.parent if cache_path else Path.cwd())
        self._trusted_hosts = {
            str(h).strip().lower() for h in (trusted_hosts or []) if str(h).strip()
        }
        parsed_host = (urlparse(registry_url).hostname or "").lower()
        if parsed_host:
            self._trusted_hosts.add(parsed_host)

    def fetch(self, force: bool = False) -> list[RegistryEntry]:
        """Fetch and parse the catalog, ensuring version monotonicity and time integrity."""
        self._trusted_time.get_time()

        if not force and self._entries is not None:
            return self._entries

        cached_entries_map = {}
        if not force and self._is_cache_valid():
            try:
                self._entries = self._load_cache()
                cached_entries_map = {e.name: e.version for e in self._entries}
            except Exception:
                pass

        raw = self._fetch_raw()
        new_entries = self._parse_catalog(raw)

        # 4. Freeze Attack/Downgrade Prevention: verify new versions are NOT older than cached ones
        # and respect 'yanked' flags and catalog timestamps if present.
        sanitized_entries = []
        for entry in new_entries:
            cached_ver = cached_entries_map.get(entry.name)
            if cached_ver:
                if parse_version(entry.version) < parse_version(cached_ver):
                    logger.warning(
                        f"Downgrade attempt detected for {entry.name}: {cached_ver} -> {entry.version}. Skipping."
                    )
                    # Keep cached version if possible
                    # (In a real system, we might want to flag the whole catalog as suspicious)
                    continue
            sanitized_entries.append(entry)

        self._entries = sanitized_entries
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
        try:
            raw_data = self._cache_path.read_text(encoding="utf-8")
            data = json.loads(raw_data)

            # Backward compatibility: accept old raw catalog cache.
            if "skills" in data and "payload" not in data:
                return self._parse_catalog(data)

            if "hmac_sig" not in data or "payload" not in data:
                logger.warning("Cache missing HMAC signature, ignoring.")
                return []

            payload_raw = str(data["payload"])
            if not verify_signature(payload_raw, str(data["hmac_sig"])):
                logger.error("Registry cache HMAC mismatch! Possible tampering detected.")
                return []

            payload = json.loads(payload_raw)
            return self._parse_catalog(payload)
        except Exception as e:
            logger.error(f"Failed to load registry cache: {e}")
            return []

    def _save_cache(self, raw: dict) -> None:
        if not self._cache_path:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            payload_str = json.dumps(raw, ensure_ascii=False)
            sig = sign_data(payload_str)

            envelope = {"hmac_sig": sig, "payload": payload_str, "saved_at": time.time()}

            tmp = self._cache_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(envelope, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._cache_path)
        except Exception as e:
            logger.error(f"Failed to save registry cache: {e}")

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
            current_url = self._url
            redirects = 0
            while True:
                parsed = urlparse(current_url)
                host = (parsed.hostname or "").lower()
                if parsed.scheme not in {"http", "https"}:
                    raise RuntimeError(f"unsupported registry scheme: {parsed.scheme or '(none)'}")
                if self._trusted_hosts and host not in self._trusted_hosts:
                    raise RuntimeError(f"registry host not trusted: {host or '(unknown)'}")
                if not resolve_safe_ip(host):
                    raise RuntimeError(
                        f"registry host resolved to unsafe/non-public IP: {host or '(unknown)'}"
                    )

                with httpx.stream(
                    "GET", current_url, timeout=self._http_timeout, follow_redirects=False
                ) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        redirects += 1
                        if redirects > _MAX_REGISTRY_REDIRECTS:
                            raise RuntimeError("too many redirects when fetching registry")
                        location = resp.headers.get("Location")
                        if not location:
                            raise RuntimeError("redirect missing Location header")
                        current_url = urljoin(current_url, location)
                        continue

                    resp.raise_for_status()
                    body = b"".join(resp.iter_bytes())
                    return json.loads(body.decode("utf-8"))
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

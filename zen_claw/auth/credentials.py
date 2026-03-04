"""Fernet-based credential vault for per-platform secrets."""

from __future__ import annotations

import base64
import json
import os
import re
import stat
from pathlib import Path


class CredentialVault:
    """Encrypted key-value store partitioned by platform."""

    _KEY_ENV_VAR = "zen_claw_VAULT_KEY"
    _KEY_FILE_NAME = "vault.key"

    def __init__(self, data_dir: Path | None = None):
        if data_dir is None:
            from zen_claw.config.loader import get_data_dir

            data_dir = get_data_dir()
        self._data_dir = Path(data_dir)
        self._cred_dir = self._data_dir / "credentials"
        self._fernet = None

    @classmethod
    def _load_or_generate_key(cls, key_dir: Path | None = None) -> bytes:
        env_key = os.environ.get(cls._KEY_ENV_VAR, "").strip()
        if env_key:
            try:
                raw = base64.urlsafe_b64decode(env_key + "==")
            except Exception as exc:
                raise ValueError(f"Invalid {cls._KEY_ENV_VAR}: {exc}") from exc
            if len(raw) != 32:
                raise ValueError(f"Invalid {cls._KEY_ENV_VAR}: decoded length must be 32, got {len(raw)}")
            return env_key.encode("ascii")

        key_file = (key_dir if key_dir is not None else Path.home() / ".zen-claw") / cls._KEY_FILE_NAME
        if key_file.exists():
            stored = key_file.read_text(encoding="ascii").strip()
            try:
                raw = base64.urlsafe_b64decode(stored + "==")
            except Exception as exc:
                raise RuntimeError(f"Failed to parse vault key file {key_file}: {exc}") from exc
            if len(raw) != 32:
                raise RuntimeError(f"Stored vault key is corrupted: expected 32 bytes, got {len(raw)}")
            return stored.encode("ascii")

        return cls._generate_and_save_key(key_file)

    @classmethod
    def _generate_and_save_key(cls, key_file: Path) -> bytes:
        from cryptography.fernet import Fernet

        key_file.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        key_file.write_bytes(key)
        try:
            os.chmod(key_file, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        return key

    def _get_fernet(self):
        if self._fernet is None:
            from cryptography.fernet import Fernet

            self._fernet = Fernet(self._load_or_generate_key(key_dir=self._data_dir))
        return self._fernet

    def _platform_file(self, platform: str) -> Path:
        # Whitelist: only alphanumerics, hyphens and underscores — blocks all
        # Windows reserved characters (: * ? < > | NUL CON …) and path separators.
        safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(platform or ""))
        return self._cred_dir / f"{safe}.json"

    def _load_platform(self, platform: str) -> dict[str, str]:
        path = self._platform_file(platform)
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save_platform(self, platform: str, data: dict[str, str]) -> None:
        self._cred_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self._cred_dir, stat.S_IRWXU)
        except OSError:
            pass
        path = self._platform_file(platform)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    def store(self, platform: str, key: str, value: str) -> None:
        platform = str(platform or "").strip().lower()
        key = str(key or "").strip()
        if not platform or not key:
            raise ValueError("platform and key must be non-empty")
        if not isinstance(value, str):
            raise ValueError("value must be a string")
        encrypted = self._get_fernet().encrypt(value.encode("utf-8"))
        blob = self._load_platform(platform)
        blob[key] = base64.urlsafe_b64encode(encrypted).decode("ascii")
        self._save_platform(platform, blob)

    def get(self, platform: str, key: str) -> str | None:
        platform = str(platform or "").strip().lower()
        key = str(key or "").strip()
        if not platform or not key:
            return None
        blob = self._load_platform(platform)
        raw = blob.get(key)
        if raw is None:
            return None
        try:
            encrypted = base64.urlsafe_b64decode(raw.encode("ascii"))
            return self._get_fernet().decrypt(encrypted).decode("utf-8")
        except Exception:
            return None

    def delete(self, platform: str, key: str) -> bool:
        platform = str(platform or "").strip().lower()
        key = str(key or "").strip()
        blob = self._load_platform(platform)
        if key not in blob:
            return False
        del blob[key]
        if blob:
            self._save_platform(platform, blob)
        else:
            path = self._platform_file(platform)
            try:
                path.unlink()
            except OSError:
                pass
        return True

    def list_platforms(self) -> list[str]:
        if not self._cred_dir.exists():
            return []
        out: list[str] = []
        for p in self._cred_dir.iterdir():
            if p.is_file() and p.suffix == ".json":
                out.append(p.stem)
        return sorted(out)

    def list_keys(self, platform: str) -> list[str]:
        return sorted(self._load_platform(platform).keys())


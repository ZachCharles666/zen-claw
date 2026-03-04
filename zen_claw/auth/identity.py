"""Ed25519 keypair-based agent identity."""

from __future__ import annotations

import base64
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class AgentIdentityError(Exception):
    """Raised for identity-related failures."""


class AgentIdentity:
    _PRIVATE_KEY_FILENAME = "identity.pem"
    _PUBLIC_META_FILENAME = "identity.pub"

    def __init__(self, key_dir: Path):
        self._key_dir = Path(key_dir).expanduser().resolve()
        self._private_key: Any = None
        self._pub_hex: str | None = None
        self._created_at: str | None = None

    def get_or_create_keypair(self) -> tuple[str, Path]:
        path = self._key_dir / self._PRIVATE_KEY_FILENAME
        if path.exists():
            self._load_existing(path)
        else:
            self._generate_new(path)
        return self.public_key_hex(), path

    def sign(self, message: bytes) -> str:
        if self._private_key is None:
            raise AgentIdentityError("Keypair not loaded. Call get_or_create_keypair() first.")
        sig = self._private_key.sign(message)
        return base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")

    def public_key_hex(self) -> str:
        try:
            from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        except Exception as exc:
            raise AgentIdentityError("cryptography package is required for identity features") from exc
        if self._private_key is None:
            raise AgentIdentityError("Keypair not loaded. Call get_or_create_keypair() first.")
        if self._pub_hex is None:
            self._pub_hex = self._private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        return self._pub_hex

    def created_at(self) -> str | None:
        return self._created_at

    @staticmethod
    def verify(public_key_hex: str, message: bytes, signature_b64: str) -> bool:
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            pub = bytes.fromhex(str(public_key_hex or ""))
            if len(pub) != 32:
                return False
            sig_raw = str(signature_b64 or "")
            pad = 4 - (len(sig_raw) % 4)
            if pad != 4:
                sig_raw += "=" * pad
            sig = base64.urlsafe_b64decode(sig_raw.encode("ascii"))
            if len(sig) != 64:
                return False
            Ed25519PublicKey.from_public_bytes(pub).verify(sig, message)
            return True
        except Exception:
            return False

    def _load_existing(self, path: Path) -> None:
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
        except Exception as exc:
            raise AgentIdentityError("cryptography package is required for identity features") from exc
        self._check_key_permissions(path)
        try:
            key = load_pem_private_key(path.read_bytes(), password=None)
        except Exception as exc:
            raise AgentIdentityError(f"Failed to load private key from {path}: {exc}") from exc
        if not isinstance(key, Ed25519PrivateKey):
            raise AgentIdentityError(f"Key at {path} is not an ed25519 private key")
        self._private_key = key
        self._pub_hex = None
        meta_path = self._key_dir / self._PUBLIC_META_FILENAME
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                self._created_at = str(meta.get("created_at") or "")
            except Exception:
                pass

    def _generate_new(self, path: Path) -> None:
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            from cryptography.hazmat.primitives.serialization import (
                Encoding,
                NoEncryption,
                PrivateFormat,
                PublicFormat,
            )
        except Exception as exc:
            raise AgentIdentityError("cryptography package is required for identity features") from exc
        self._key_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self._key_dir, 0o700)
        except Exception:
            pass
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
        path.write_bytes(pem)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        self._private_key = key
        self._pub_hex = None
        self._created_at = datetime.now(UTC).isoformat()
        pub_hex = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        meta = {
            "public_key_hex": pub_hex,
            "created_at": self._created_at,
            "algorithm": "ed25519",
            "note": "zen-claw agent identity. This file is not secret.",
        }
        (self._key_dir / self._PUBLIC_META_FILENAME).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def _check_key_permissions(path: Path) -> None:
        if os.name == "nt":
            return
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & (stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH):
            raise AgentIdentityError(
                f"Private key {path} has insecure permissions (mode {oct(mode)}). Run: chmod 600 {path}"
            )

"""User store with bcrypt password hashes."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Optional

from loguru import logger

from zen_claw.auth.paths import _validate_tenant_id


@dataclass
class User:
    user_id: str
    tenant_id: str
    username: str
    password_hash: str
    role: Literal["admin", "member"]
    created_at: float
    enabled: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "User":
        return cls(
            user_id=str(d["user_id"]),
            tenant_id=str(d["tenant_id"]),
            username=str(d["username"]),
            password_hash=str(d["password_hash"]),
            role=str(d.get("role", "member")),  # type: ignore[arg-type]
            created_at=float(d.get("created_at", 0)),
            enabled=bool(d.get("enabled", True)),
        )

    def safe_dict(self) -> dict:
        data = self.to_dict()
        data.pop("password_hash", None)
        return data


def _hash_password(plaintext: str) -> str:
    import bcrypt

    return bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(plaintext: str, hashed: str) -> bool:
    try:
        import bcrypt

        return bcrypt.checkpw(plaintext.encode("utf-8"), hashed.encode("utf-8"))
    except Exception as exc:
        logger.error(f"Password verify error: {exc}")
        return False


class UserStore:
    def __init__(self, base_data_dir: Path) -> None:
        self.base_data_dir = Path(base_data_dir)

    def _users_dir(self, tenant_id: str) -> Path:
        _validate_tenant_id(tenant_id)
        return self.base_data_dir / "tenants" / tenant_id / "users"

    def _user_file(self, tenant_id: str, user_id: str) -> Path:
        return self._users_dir(tenant_id) / f"{user_id}.json"

    def create(
        self,
        tenant_id: str,
        username: str,
        password: str,
        role: Literal["admin", "member"] = "member",
    ) -> User:
        if not username or not username.strip():
            raise ValueError("Username cannot be empty")
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters")
        if self.get_by_username(tenant_id, username):
            raise ValueError(f"Username '{username}' already exists in this tenant")
        user = User(
            user_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            username=username.strip().lower(),
            password_hash=_hash_password(password),
            role=role,
            created_at=time.time(),
        )
        self._write(user)
        return user

    def authenticate(
        self, username: str, password: str, tenant_id: str | None = None
    ) -> Optional[User]:
        tenants = [tenant_id] if tenant_id else self._all_tenant_ids()
        for tid in tenants:
            if not tid:
                continue
            user = self.get_by_username(tid, username)
            if user and user.enabled and _verify_password(password, user.password_hash):
                return user
        return None

    def get(self, tenant_id: str, user_id: str) -> Optional[User]:
        f = self._user_file(tenant_id, user_id)
        if not f.exists():
            return None
        try:
            return User.from_dict(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            return None

    def get_by_username(self, tenant_id: str, username: str) -> Optional[User]:
        target = username.strip().lower()
        for user in self.list_by_tenant(tenant_id):
            if user.username == target:
                return user
        return None

    def list_by_tenant(self, tenant_id: str) -> list[User]:
        users_dir = self._users_dir(tenant_id)
        if not users_dir.exists():
            return []
        rows: list[User] = []
        for f in users_dir.glob("*.json"):
            try:
                rows.append(User.from_dict(json.loads(f.read_text(encoding="utf-8"))))
            except Exception:
                continue
        return sorted(rows, key=lambda x: x.created_at)

    def delete(self, tenant_id: str, user_id: str) -> bool:
        user = self.get(tenant_id, user_id)
        if not user:
            return False
        user.enabled = False
        self._write(user)
        return True

    def _write(self, user: User) -> None:
        f = self._user_file(user.tenant_id, user.user_id)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(user.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    def _all_tenant_ids(self) -> list[str]:
        root = self.base_data_dir / "tenants"
        if not root.exists():
            return []
        return [d.name for d in root.iterdir() if d.is_dir()]

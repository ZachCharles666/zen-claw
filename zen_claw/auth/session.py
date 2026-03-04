"""JWT session manager."""

from __future__ import annotations

import time
from typing import Optional

from loguru import logger


class SessionManager:
    def __init__(self, secret: str, algorithm: str = "HS256", expire_seconds: int = 86400) -> None:
        if not secret:
            raise ValueError("JWT secret cannot be empty.")
        self.secret = secret
        self.algorithm = algorithm
        self.expire_seconds = expire_seconds

    def create_session(self, user_id: str, tenant_id: str, username: str, role: str) -> str:
        try:
            import jwt
        except ImportError as exc:
            raise RuntimeError("PyJWT is not installed") from exc
        now = int(time.time())
        payload = {"sub": user_id, "tid": tenant_id, "username": username, "role": role, "iat": now, "exp": now + self.expire_seconds}
        return jwt.encode(payload, self.secret, algorithm=self.algorithm)

    def validate_session(self, token: str) -> Optional[dict]:
        try:
            import jwt
        except ImportError:
            logger.error("PyJWT is not installed; JWT validation is unavailable")
            return None

        try:
            return jwt.decode(
                token,
                self.secret,
                algorithms=[self.algorithm],
                options={"require": ["sub", "tid", "exp"]},
            )
        except jwt.ExpiredSignatureError:
            logger.debug("JWT validation failed: token has expired")
        except jwt.InvalidTokenError as exc:
            logger.debug("JWT validation failed: invalid token — {}", exc)
        except Exception as exc:
            logger.error("JWT validation raised unexpected error: {}", exc)
        return None

"""FastAPI auth middleware for multi-tenant mode."""

from __future__ import annotations

from typing import Iterable
from urllib.parse import quote

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse

from zen_claw.auth.session import SessionManager


class MultiTenantAuthMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        session_manager: SessionManager,
        public_paths: Iterable[str] | None = None,
        login_path: str = "/login",
        cookie_name: str = "nc_session",
    ):
        super().__init__(app)
        self.session_manager = session_manager
        self.public_paths = list(public_paths or ["/login", "/api/v1/health", "/static/"])
        self.login_path = login_path
        self.cookie_name = cookie_name

    def _is_public(self, path: str) -> bool:
        for p in self.public_paths:
            if p.endswith("/") and path.startswith(p):
                return True
            if path == p:
                return True
        return False

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if self._is_public(path):
            return await call_next(request)
        token = request.cookies.get(self.cookie_name)
        if not token:
            auth = request.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                token = auth[7:].strip()
        if not token:
            return RedirectResponse(
                url=f"{self.login_path}?next={quote(path, safe='')}", status_code=302
            )
        payload = self.session_manager.validate_session(token)
        if payload is None:
            return RedirectResponse(
                url=f"{self.login_path}?next={quote(path, safe='')}", status_code=302
            )
        request.state.user = payload
        request.state.tenant_id = payload.get("tid")
        return await call_next(request)

from __future__ import annotations

import json

import httpx

from zen_claw.agent.tools.result import ToolErrorKind
from zen_claw.agent.tools.web import USER_AGENT, WebFetchTool


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        content_type: str = "application/json",
        text: str = '{"ok": true}',
        url: str = "https://example.com",
    ) -> None:
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self.text = text
        self.url = url

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", self.url)
            response = httpx.Response(
                self.status_code,
                request=request,
                text=self.text,
                headers=self.headers,
            )
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=request,
                response=response,
            )


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.last_headers: dict[str, str] = {}

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def get(self, url: str, headers: dict[str, str]):
        self.last_headers = headers
        return self._response


async def test_web_fetch_local_uses_descriptive_user_agent(monkeypatch) -> None:
    fake = _FakeClient(_FakeResponse())
    monkeypatch.setattr(
        "zen_claw.agent.tools.web.httpx.AsyncClient",
        lambda **kwargs: fake,
    )

    tool = WebFetchTool()
    result = await tool.execute("https://example.com/data.json")

    assert result.ok is True
    assert fake.last_headers["User-Agent"] == USER_AGENT
    assert "application/json" in fake.last_headers["Accept"]


async def test_web_fetch_local_http_403_returns_permission(monkeypatch) -> None:
    fake = _FakeClient(
        _FakeResponse(
            status_code=403,
            content_type="text/plain",
            text="Please respect our robot policy",
            url="https://en.wikipedia.org/api/rest_v1/page/summary/Alan%20Turing",
        )
    )
    monkeypatch.setattr(
        "zen_claw.agent.tools.web.httpx.AsyncClient",
        lambda **kwargs: fake,
    )

    tool = WebFetchTool()
    result = await tool.execute("https://en.wikipedia.org/api/rest_v1/page/summary/Alan%20Turing")

    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.PERMISSION
    assert result.error.code == "web_fetch_http_403"
    assert "robot policy" in result.error.message


async def test_web_fetch_local_http_503_returns_retryable(monkeypatch) -> None:
    fake = _FakeClient(
        _FakeResponse(
            status_code=503,
            content_type="text/plain",
            text="upstream unavailable",
            url="https://example.com/unavailable",
        )
    )
    monkeypatch.setattr(
        "zen_claw.agent.tools.web.httpx.AsyncClient",
        lambda **kwargs: fake,
    )

    tool = WebFetchTool()
    result = await tool.execute("https://example.com/unavailable")

    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.RETRYABLE
    assert result.error.code == "web_fetch_http_503"

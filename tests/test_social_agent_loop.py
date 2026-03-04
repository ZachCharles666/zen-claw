import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from zen_claw.agent.tools.result import ToolErrorKind
from zen_claw.agent.tools.social_platform import (
    SocialPlatformGetTool,
    SocialPlatformLikeTool,
    SocialPlatformPostTool,
)


class _FakeResp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self, response: _FakeResp):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, headers, json):
        return self._response


async def test_social_post_tool_success(monkeypatch):
    payload = {"ok": True, "status": 201, "body": json.dumps({"id": "post_123", "status": "created"})}
    monkeypatch.setattr(
        "zen_claw.agent.tools.social_platform.httpx.AsyncClient",
        lambda timeout: _FakeClient(_FakeResp(200, payload)),
    )
    tool = SocialPlatformPostTool()
    result = await tool.execute(
        base_url="https://moltbook.example.com",
        endpoint="/api/posts",
        payload={"title": "Hello"},
        auth_header="Bearer token",
    )
    assert result.ok is True
    data = json.loads(result.content)
    assert data["id"] == "post_123"


async def test_social_post_tool_invalid_base_url():
    tool = SocialPlatformPostTool()
    result = await tool.execute(
        base_url="ftp://bad.example.com",
        endpoint="/api/posts",
        payload={},
        auth_header="Bearer token",
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.PARAMETER
    assert result.error.code == "invalid_base_url"


async def test_social_post_tool_auth_failure(monkeypatch):
    payload = {"ok": False, "status": 401, "error": "Unauthorized"}
    monkeypatch.setattr(
        "zen_claw.agent.tools.social_platform.httpx.AsyncClient",
        lambda timeout: _FakeClient(_FakeResp(401, payload)),
    )
    tool = SocialPlatformPostTool()
    result = await tool.execute(
        base_url="https://moltbook.example.com",
        endpoint="/api/posts",
        payload={"body": "hello"},
        auth_header="Bearer bad",
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.PERMISSION


async def test_social_get_tool_success(monkeypatch):
    posts = [{"id": "1", "title": "Test"}, {"id": "2", "title": "Another"}]
    payload = {"ok": True, "status": 200, "body": json.dumps({"posts": posts})}
    monkeypatch.setattr(
        "zen_claw.agent.tools.social_platform.httpx.AsyncClient",
        lambda timeout: _FakeClient(_FakeResp(200, payload)),
    )
    tool = SocialPlatformGetTool()
    result = await tool.execute(
        base_url="https://moltbook.example.com",
        endpoint="/api/posts",
        auth_header="Bearer token",
        query_params={"submolt": "python"},
    )
    assert result.ok is True
    data = json.loads(result.content)
    assert data["posts"][0]["id"] == "1"


async def test_social_get_tool_endpoint_normalizes_slash():
    base = "https://example.com"
    endpoint = "api/posts"
    if not endpoint.startswith("/"):
        endpoint = "/" + endpoint
    assert base.rstrip("/") + endpoint == "https://example.com/api/posts"


def _make_loop(tmp_path: Path, dry_run: bool = False):
    from zen_claw.agent.social_loop import SocialAgentLoop, SocialPlatformConfig

    cfg = SocialPlatformConfig(
        platform="moltbook",
        base_url="https://moltbook.example.com",
        submolt="test",
        auth_header="Bearer token",
        max_posts_per_cycle=5,
        dry_run=dry_run,
    )
    provider = MagicMock()
    config = MagicMock()
    config.agents.defaults.model = "test/model"
    return SocialAgentLoop(config=config, platform_config=cfg, provider=provider, workspace=tmp_path, model="test/model")


async def test_filter_interesting_returns_subset(tmp_path: Path):
    loop = _make_loop(tmp_path)
    posts = [
        {"id": "1", "title": "How to do X?", "body": "...", "author": "alice"},
        {"id": "2", "title": "lol", "body": "spam", "author": "bot"},
    ]
    resp = MagicMock()
    resp.content = '["1"]'
    loop._provider.chat = AsyncMock(return_value=resp)
    out = await loop._filter_interesting(posts)
    assert len(out) == 1
    assert out[0]["id"] == "1"


async def test_filter_interesting_empty_posts(tmp_path: Path):
    loop = _make_loop(tmp_path)
    loop._provider.chat = AsyncMock()
    out = await loop._filter_interesting([])
    assert out == []
    loop._provider.chat.assert_not_called()


async def test_filter_interesting_llm_bad_json(tmp_path: Path):
    loop = _make_loop(tmp_path)
    resp = MagicMock()
    resp.content = "post 1 looks good"
    loop._provider.chat = AsyncMock(return_value=resp)
    out = await loop._filter_interesting([{"id": "1", "title": "Q"}])
    assert out == []


async def test_memory_recording(tmp_path: Path):
    loop = _make_loop(tmp_path)
    await loop._record_interaction("post_99", "comment", "My test response.")
    today = loop._memory.read_today()
    assert "[social:moltbook:post:post_99]" in today
    assert "My test response." in today


async def test_already_responded_true(tmp_path: Path):
    loop = _make_loop(tmp_path)
    await loop._record_interaction("post_77", "comment", "Previous response.")
    assert await loop._already_responded("post_77") is True


async def test_already_responded_false(tmp_path: Path):
    loop = _make_loop(tmp_path)
    assert await loop._already_responded("post_unseen") is False


async def test_run_once_dry_run_no_post(tmp_path: Path):
    loop = _make_loop(tmp_path, dry_run=True)
    posts = [{"id": "10", "title": "Question", "body": "Help", "author": "u1"}]
    loop._fetch_new_posts = AsyncMock(return_value=posts)
    loop._filter_interesting = AsyncMock(return_value=posts)
    loop._compose_response = AsyncMock(return_value="Helpful response")
    loop._post_response = AsyncMock(return_value=True)
    result = await loop.run_once()
    assert result.dry_run is True
    assert result.responses_composed == 1
    assert result.responses_posted == 0
    loop._post_response.assert_not_called()
    assert "dry_run_comment" in loop._memory.read_today()


async def test_run_once_skips_duplicate(tmp_path: Path):
    loop = _make_loop(tmp_path, dry_run=False)
    posts = [{"id": "55", "title": "Old", "body": "...", "author": "u1"}]
    await loop._record_interaction("55", "comment", "already")
    loop._fetch_new_posts = AsyncMock(return_value=posts)
    loop._filter_interesting = AsyncMock(return_value=posts)
    loop._compose_response = AsyncMock(return_value="new")
    loop._post_response = AsyncMock(return_value=True)
    result = await loop.run_once()
    assert result.responses_composed == 0
    loop._post_response.assert_not_called()


# ── SocialPlatformLikeTool tests ─────────────────────────────────────────────

async def test_like_tool_success(monkeypatch):
    payload = {"ok": True, "status": 200, "body": json.dumps({"liked": True})}
    monkeypatch.setattr(
        "zen_claw.agent.tools.social_platform.httpx.AsyncClient",
        lambda timeout: _FakeClient(_FakeResp(200, payload)),
    )
    tool = SocialPlatformLikeTool()
    result = await tool.execute(
        base_url="https://moltbook.example.com",
        post_id="post_42",
        auth_header="Bearer token",
    )
    assert result.ok is True
    data = json.loads(result.content)
    assert data["liked"] is True


async def test_like_tool_missing_post_id():
    tool = SocialPlatformLikeTool()
    result = await tool.execute(
        base_url="https://moltbook.example.com",
        post_id="",
        auth_header="Bearer token",
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "missing_post_id"


async def test_like_tool_auth_failure(monkeypatch):
    payload = {"ok": False, "status": 401, "error": "Unauthorized"}
    monkeypatch.setattr(
        "zen_claw.agent.tools.social_platform.httpx.AsyncClient",
        lambda timeout: _FakeClient(_FakeResp(401, payload)),
    )
    tool = SocialPlatformLikeTool()
    result = await tool.execute(
        base_url="https://moltbook.example.com",
        post_id="post_99",
        auth_header="Bearer bad",
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.PERMISSION


async def test_run_once_calls_like_before_reply(tmp_path: Path):
    """run_once should call _maybe_like_post for each filtered post."""
    loop = _make_loop(tmp_path, dry_run=True)
    posts = [{"id": "77", "title": "Like me", "body": "...", "author": "u1"}]
    loop._fetch_new_posts = AsyncMock(return_value=posts)
    loop._filter_interesting = AsyncMock(return_value=posts)
    loop._compose_response = AsyncMock(return_value="Great post!")
    loop._post_response = AsyncMock(return_value=True)
    loop._maybe_like_post = AsyncMock()
    result = await loop.run_once()
    loop._maybe_like_post.assert_awaited_once_with("77")
    assert result.responses_composed == 1

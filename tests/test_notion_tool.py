"""Tests for NotionTool."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from zen_claw.agent.tools.notion import NotionTool, _blocks_to_text, _content_to_blocks
from zen_claw.agent.tools.result import ToolErrorKind
from zen_claw.config.schema import NotionConfig


def _make_config(enabled: bool = True) -> NotionConfig:
    return NotionConfig(enabled=enabled, credential_platform="notion", credential_key="token")


def _make_tool(enabled: bool = True) -> NotionTool:
    cfg = _make_config(enabled=enabled)
    vault = MagicMock()
    vault.get.return_value = "secret_notion_token"
    tool = NotionTool(config=cfg, vault=vault)
    return tool


def _make_notion_client_mock() -> MagicMock:
    client = MagicMock()
    return client


# ── Helper tests ───────────────────────────────────────────────────────────────


def test_blocks_to_text_paragraph():
    blocks = [{"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Hello world"}]}}]
    assert _blocks_to_text(blocks) == "Hello world"


def test_blocks_to_text_heading():
    blocks = [{"type": "heading_2", "heading_2": {"rich_text": [{"plain_text": "Section"}]}}]
    assert "## Section" in _blocks_to_text(blocks)


def test_blocks_to_text_todo():
    blocks = [
        {"type": "to_do", "to_do": {"rich_text": [{"plain_text": "Task A"}], "checked": False}},
        {"type": "to_do", "to_do": {"rich_text": [{"plain_text": "Task B"}], "checked": True}},
    ]
    text = _blocks_to_text(blocks)
    assert "[ ] Task A" in text
    assert "[x] Task B" in text


def test_blocks_to_text_empty():
    assert _blocks_to_text([]) == ""


def test_content_to_blocks_basic():
    blocks = _content_to_blocks("Line 1\nLine 2")
    assert len(blocks) == 2
    assert blocks[0]["type"] == "paragraph"
    texts = [b["paragraph"]["rich_text"][0]["text"]["content"] for b in blocks]
    assert "Line 1" in texts
    assert "Line 2" in texts


def test_content_to_blocks_empty():
    blocks = _content_to_blocks("")
    assert len(blocks) == 1  # returns one empty paragraph


# ── Config validation ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_returns_permission_error():
    tool = _make_tool(enabled=False)
    result = await tool.execute(action="search", query="test")
    assert not result.ok
    assert result.error.kind == ToolErrorKind.PERMISSION
    assert "notion_not_configured" in ((result.error.code if result.error else "") or "")


@pytest.mark.asyncio
async def test_missing_dep_returns_graceful_error():
    tool = _make_tool()
    with patch.dict("sys.modules", {"notion_client": None}):
        result = await tool.execute(action="search", query="test")
    assert not result.ok
    assert result.error.kind == ToolErrorKind.PERMISSION


@pytest.mark.asyncio
async def test_missing_token_returns_setup_error():
    cfg = _make_config(enabled=True)
    vault = MagicMock()
    vault.get.return_value = None  # no token
    tool = NotionTool(config=cfg, vault=vault)
    mock_notion = MagicMock()
    with patch.dict("sys.modules", {"notion_client": mock_notion}):
        result = await tool.execute(action="search", query="test")
    assert not result.ok


# ── Action tests ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_missing_query():
    tool = _make_tool()
    result = await tool.execute(action="search")
    assert not result.ok
    assert "notion_missing_query" in ((result.error.code if result.error else "") or "")


@pytest.mark.asyncio
async def test_read_page_missing_id():
    tool = _make_tool()
    result = await tool.execute(action="read_page")
    assert not result.ok
    assert "notion_missing_page_id" in ((result.error.code if result.error else "") or "")


@pytest.mark.asyncio
async def test_create_page_missing_parent():
    tool = _make_tool()
    result = await tool.execute(action="create_page", title="Test")
    assert not result.ok
    assert "notion_missing_parent" in ((result.error.code if result.error else "") or "")


@pytest.mark.asyncio
async def test_query_database_missing_id():
    tool = _make_tool()
    result = await tool.execute(action="query_database")
    assert not result.ok
    assert "notion_missing_db_id" in ((result.error.code if result.error else "") or "")


@pytest.mark.asyncio
async def test_create_database_item_missing_properties():
    tool = _make_tool()
    result = await tool.execute(action="create_database_item", database_id="db123")
    assert not result.ok
    assert "notion_missing_properties" in ((result.error.code if result.error else "") or "")


@pytest.mark.asyncio
async def test_search_returns_page_list():
    tool = _make_tool()
    mock_client = MagicMock()
    mock_client.search.return_value = {
        "results": [
            {
                "object": "page",
                "id": "page-001",
                "properties": {
                    "title": {"title": [{"plain_text": "My Page"}]}
                },
            }
        ]
    }
    tool._client = mock_client
    result = await tool.execute(action="search", query="My")
    assert result.ok
    assert "page-001" in result.content
    assert "My Page" in result.content


@pytest.mark.asyncio
async def test_read_page_converts_blocks_to_text():
    tool = _make_tool()
    mock_client = MagicMock()
    mock_client.blocks.children.list.return_value = {
        "results": [
            {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Hello Notion"}]}}
        ]
    }
    tool._client = mock_client
    result = await tool.execute(action="read_page", page_id="page-001")
    assert result.ok
    assert "Hello Notion" in result.content


@pytest.mark.asyncio
async def test_query_database_passes_filter():
    tool = _make_tool()
    mock_client = MagicMock()
    mock_client.databases.query.return_value = {
        "results": [
            {
                "id": "item-001",
                "properties": {"Name": {"type": "title", "title": [{"plain_text": "Task 1"}]}},
            }
        ],
        "has_more": False,
    }
    tool._client = mock_client
    filter_obj = {"property": "Status", "select": {"equals": "Done"}}
    result = await tool.execute(action="query_database", database_id="db-001", filter=filter_obj)
    assert result.ok
    assert "Task 1" in result.content
    mock_client.databases.query.assert_called_once()
    call_kwargs = mock_client.databases.query.call_args[1]
    assert call_kwargs.get("filter") == filter_obj


@pytest.mark.asyncio
async def test_unknown_action():
    tool = _make_tool()
    result = await tool.execute(action="teleport")
    assert not result.ok
    assert "notion_unknown_action" in ((result.error.code if result.error else "") or "")

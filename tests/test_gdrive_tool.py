"""Tests for GDriveTool."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from zen_claw.agent.tools.gdrive import GDriveTool, _GOOGLE_DOC_MIME, _GOOGLE_SHEET_MIME
from zen_claw.agent.tools.result import ToolErrorKind
from zen_claw.config.schema import GDriveConfig


def _make_config(enabled: bool = True) -> GDriveConfig:
    return GDriveConfig(
        enabled=enabled,
        token_file="~/.zen-claw/google_token.json",
        credentials_file="~/.zen-claw/google_credentials.json",
    )


def _make_tool(enabled: bool = True) -> GDriveTool:
    cfg = _make_config(enabled=enabled)
    tool = GDriveTool(config=cfg)
    return tool


def _mock_service() -> MagicMock:
    return MagicMock()


# ── Config / permission tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_returns_permission_error():
    tool = _make_tool(enabled=False)
    result = await tool.execute(action="list_files")
    assert not result.ok
    assert result.error.kind == ToolErrorKind.PERMISSION
    assert "gdrive_not_configured" in ((result.error.code if result.error else "") or "")


@pytest.mark.asyncio
async def test_missing_dep_returns_setup_error():
    tool = _make_tool()
    with patch(
        "zen_claw.agent.tools.gdrive._load_google_credentials",
        side_effect=RuntimeError("google-api-python-client not installed"),
    ):
        result = await tool.execute(action="list_files")
    assert not result.ok
    assert result.error.kind == ToolErrorKind.PERMISSION


# ── Parameter validation ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_file_missing_id():
    tool = _make_tool()
    result = await tool.execute(action="read_file")
    assert not result.ok
    assert "gdrive_missing_file_id" in ((result.error.code if result.error else "") or "")


@pytest.mark.asyncio
async def test_upload_file_missing_path():
    tool = _make_tool()
    result = await tool.execute(action="upload_file")
    assert not result.ok
    assert "gdrive_missing_path" in ((result.error.code if result.error else "") or "")


@pytest.mark.asyncio
async def test_search_missing_query():
    tool = _make_tool()
    result = await tool.execute(action="search_files")
    assert not result.ok
    assert "gdrive_missing_query" in ((result.error.code if result.error else "") or "")


@pytest.mark.asyncio
async def test_create_folder_missing_name():
    tool = _make_tool()
    result = await tool.execute(action="create_folder")
    assert not result.ok
    assert "gdrive_missing_name" in ((result.error.code if result.error else "") or "")


@pytest.mark.asyncio
async def test_unknown_action():
    tool = _make_tool()
    result = await tool.execute(action="beam_up")
    assert not result.ok
    assert "gdrive_unknown_action" in ((result.error.code if result.error else "") or "")


# ── list_files ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_files_returns_formatted_list():
    tool = _make_tool()
    svc = _mock_service()
    svc.files().list().execute.return_value = {
        "files": [
            {
                "id": "file-001",
                "name": "report.pdf",
                "mimeType": "application/pdf",
                "modifiedTime": "2026-03-27T10:00:00Z",
                "size": "12345",
            }
        ]
    }
    tool._service = svc
    result = await tool.execute(action="list_files")
    assert result.ok
    assert "report.pdf" in result.content
    assert "file-001" in result.content


@pytest.mark.asyncio
async def test_list_files_empty():
    tool = _make_tool()
    svc = _mock_service()
    svc.files().list().execute.return_value = {"files": []}
    tool._service = svc
    result = await tool.execute(action="list_files")
    assert result.ok
    assert "No files" in result.content


# ── read_file ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_file_google_doc_uses_export():
    tool = _make_tool()
    svc = _mock_service()
    svc.files().get().execute.return_value = {"name": "doc.gdoc", "mimeType": _GOOGLE_DOC_MIME}
    svc.files().export().execute.return_value = b"Document content here"
    tool._service = svc
    result = await tool.execute(action="read_file", file_id="doc-001")
    assert result.ok
    assert "Document content here" in result.content
    svc.files().export.assert_called()


@pytest.mark.asyncio
async def test_read_file_google_sheet_exports_csv():
    tool = _make_tool()
    svc = _mock_service()
    svc.files().get().execute.return_value = {"name": "sheet.gsheet", "mimeType": _GOOGLE_SHEET_MIME}
    svc.files().export().execute.return_value = b"col1,col2\nval1,val2"
    tool._service = svc
    result = await tool.execute(action="read_file", file_id="sheet-001")
    assert result.ok
    assert "col1" in result.content


@pytest.mark.asyncio
async def test_read_file_binary_returns_message():
    tool = _make_tool()
    svc = _mock_service()
    svc.files().get().execute.return_value = {
        "name": "image.png",
        "mimeType": "image/png",
    }
    tool._service = svc
    result = await tool.execute(action="read_file", file_id="img-001")
    assert result.ok
    assert "binary" in result.content.lower()


# ── create_folder ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_folder_returns_id():
    tool = _make_tool()
    svc = _mock_service()
    svc.files().create().execute.return_value = {"id": "folder-001", "name": "Reports"}
    tool._service = svc
    result = await tool.execute(action="create_folder", name="Reports")
    assert result.ok
    assert "folder-001" in result.content


# ── upload_file ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_file_not_found():
    tool = _make_tool()
    tool._service = _mock_service()
    result = await tool.execute(action="upload_file", local_path="/nonexistent/file.txt")
    assert not result.ok
    assert result.error.kind == ToolErrorKind.PARAMETER
    assert "gdrive_file_not_found" in ((result.error.code if result.error else "") or "")


# ── Shared credentials with Calendar ──────────────────────────────────────────


def test_gdrive_and_calendar_share_default_token_path():
    from zen_claw.config.schema import CalendarConfig

    drive_cfg = GDriveConfig()
    cal_cfg = CalendarConfig()
    assert drive_cfg.token_file == cal_cfg.google.token_file
    assert drive_cfg.credentials_file == cal_cfg.google.credentials_file

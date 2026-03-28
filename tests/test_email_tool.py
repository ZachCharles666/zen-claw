"""Tests for EmailTool."""

from __future__ import annotations

import email as email_lib
import email.mime.multipart
import email.mime.text
from unittest.mock import MagicMock, patch

import pytest

from zen_claw.agent.tools.email import EmailTool, _decode_header, _msg_to_summary
from zen_claw.agent.tools.result import ToolErrorKind
from zen_claw.config.schema import EmailConfig


def _make_config(**kwargs) -> EmailConfig:
    defaults = dict(
        enabled=True,
        imap_host="imap.example.com",
        imap_port=993,
        smtp_host="smtp.example.com",
        smtp_port=465,
        username="user@example.com",
        use_ssl=True,
        credential_platform="email",
        credential_key="password",
    )
    defaults.update(kwargs)
    return EmailConfig(**defaults)


def _make_tool(config: EmailConfig | None = None) -> EmailTool:
    cfg = config or _make_config()
    vault = MagicMock()
    vault.get.return_value = "secret123"
    return EmailTool(config=cfg, vault=vault)


# ── Unit helpers ───────────────────────────────────────────────────────────────


def test_decode_header_plain():
    assert _decode_header("Hello World") == "Hello World"


def test_decode_header_encoded():
    # UTF-8 encoded header
    encoded = "=?UTF-8?b?5L2g5aW9?="
    result = _decode_header(encoded)
    assert isinstance(result, str)
    assert len(result) > 0


def test_msg_to_summary():
    msg = email_lib.message_from_string(
        "From: Alice <alice@example.com>\r\n"
        "Subject: Test Subject\r\n"
        "Date: Mon, 27 Mar 2026 09:00:00 +0000\r\n"
        "\r\n"
        "Hello, this is the body text."
    )
    summary = _msg_to_summary(msg, "42")
    assert "42" in summary
    assert "Alice" in summary
    assert "Test Subject" in summary
    assert "Hello" in summary


# ── Config validation ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_tool_returns_permission_error():
    tool = _make_tool(_make_config(enabled=False))
    result = await tool.execute(action="read_inbox")
    assert not result.ok
    assert result.error.kind == ToolErrorKind.PERMISSION
    assert "email_not_configured" in ((result.error.code if result.error else "") or "")


@pytest.mark.asyncio
async def test_missing_imap_host_returns_permission_error():
    tool = _make_tool(_make_config(imap_host=""))
    result = await tool.execute(action="read_inbox")
    assert not result.ok
    assert result.error.kind == ToolErrorKind.PERMISSION


@pytest.mark.asyncio
async def test_send_missing_smtp_host():
    tool = _make_tool(_make_config(smtp_host=""))
    result = await tool.execute(action="send", to="bob@example.com", subject="Hi", body="Hello")
    assert not result.ok
    assert "email_no_smtp" in ((result.error.code if result.error else "") or "")


@pytest.mark.asyncio
async def test_send_missing_to():
    tool = _make_tool()
    result = await tool.execute(action="send", subject="Hi", body="Hello")
    assert not result.ok
    assert "email_missing_to" in ((result.error.code if result.error else "") or "")


@pytest.mark.asyncio
async def test_search_missing_query():
    tool = _make_tool()
    result = await tool.execute(action="search")
    assert not result.ok
    assert "email_missing_query" in ((result.error.code if result.error else "") or "")


@pytest.mark.asyncio
async def test_get_missing_uid():
    tool = _make_tool()
    result = await tool.execute(action="get")
    assert not result.ok
    assert "email_missing_uid" in ((result.error.code if result.error else "") or "")


@pytest.mark.asyncio
async def test_unknown_action():
    tool = _make_tool()
    result = await tool.execute(action="fly_to_moon")
    assert not result.ok
    assert "email_unknown_action" in ((result.error.code if result.error else "") or "")


# ── IMAP mock tests ────────────────────────────────────────────────────────────


def _make_imap_mock(uid_list: list[bytes], rfc822_data: list) -> MagicMock:
    imap = MagicMock()
    imap.select.return_value = ("OK", [b"1"])
    imap.uid.side_effect = lambda cmd, *args: (
        ("OK", [b" ".join(uid_list)])
        if cmd == "search"
        else ("OK", rfc822_data)
    )
    imap.logout.return_value = None
    return imap


def _build_raw_msg(subject: str = "Test", from_: str = "a@b.com", body: str = "body text") -> bytes:
    msg = email_lib.mime.multipart.MIMEMultipart()
    msg["From"] = from_
    msg["Subject"] = subject
    msg["Date"] = "Mon, 27 Mar 2026 09:00:00 +0000"
    msg.attach(email_lib.mime.text.MIMEText(body, "plain", "utf-8"))
    return msg.as_bytes()


@pytest.mark.asyncio
async def test_read_inbox_returns_summaries():
    tool = _make_tool()
    raw = _build_raw_msg("Weekly Report", "boss@corp.com", "Please review.")
    imap_mock = MagicMock()
    imap_mock.select.return_value = ("OK", [b"3"])
    imap_mock.uid.side_effect = [
        ("OK", [b"1 2 3"]),                     # search
        ("OK", [(b"1 (RFC822)", raw)]),           # fetch uid=3
        ("OK", [(b"2 (RFC822)", raw)]),           # fetch uid=2
        ("OK", [(b"3 (RFC822)", raw)]),           # fetch uid=1
    ]
    imap_mock.logout.return_value = None

    with patch("zen_claw.agent.tools.email.imaplib.IMAP4_SSL", return_value=imap_mock):
        result = await tool.execute(action="read_inbox", limit=3, unread_only=False)

    assert result.ok
    assert "Weekly Report" in result.content


@pytest.mark.asyncio
async def test_read_inbox_empty():
    tool = _make_tool()
    imap_mock = MagicMock()
    imap_mock.select.return_value = ("OK", [b"0"])
    imap_mock.uid.return_value = ("OK", [b""])
    imap_mock.logout.return_value = None

    with patch("zen_claw.agent.tools.email.imaplib.IMAP4_SSL", return_value=imap_mock):
        result = await tool.execute(action="read_inbox", unread_only=True)

    assert result.ok
    assert "No unread" in result.content


@pytest.mark.asyncio
async def test_mark_read_calls_store():
    tool = _make_tool()
    imap_mock = MagicMock()
    imap_mock.select.return_value = ("OK", [b"1"])
    imap_mock.uid.return_value = ("OK", [b"OK"])
    imap_mock.logout.return_value = None

    with patch("zen_claw.agent.tools.email.imaplib.IMAP4_SSL", return_value=imap_mock):
        result = await tool.execute(action="mark_read", uid="42")

    assert result.ok
    assert "42" in result.content
    imap_mock.uid.assert_called_with("store", "42", "+FLAGS", r"\Seen")


@pytest.mark.asyncio
async def test_imap_error_returns_runtime_failure():
    tool = _make_tool()
    import imaplib

    with patch(
        "zen_claw.agent.tools.email.imaplib.IMAP4_SSL",
        side_effect=imaplib.IMAP4.error("auth failed"),
    ):
        result = await tool.execute(action="read_inbox")

    assert not result.ok
    assert result.error.kind == ToolErrorKind.RUNTIME
    assert "email_imap_error" in ((result.error.code if result.error else "") or "")


# ── Password security ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_password_not_in_result():
    """Ensure vault password never appears in tool result content."""
    tool = _make_tool()
    raw = _build_raw_msg("secret", "a@b.com", "body")
    imap_mock = MagicMock()
    imap_mock.select.return_value = ("OK", [b"1"])
    imap_mock.uid.side_effect = [
        ("OK", [b"1"]),
        ("OK", [(b"1 (RFC822)", raw)]),
    ]
    imap_mock.logout.return_value = None

    with patch("zen_claw.agent.tools.email.imaplib.IMAP4_SSL", return_value=imap_mock):
        result = await tool.execute(action="read_inbox")

    assert "secret123" not in (result.content or "")

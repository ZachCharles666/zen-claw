"""Email tool — IMAP read + SMTP send via stdlib (zero new dependencies)."""

from __future__ import annotations

import asyncio
import email as email_lib
import email.header
import email.mime.multipart
import email.mime.text
import imaplib
import smtplib
from typing import TYPE_CHECKING, Any

from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult

if TYPE_CHECKING:
    from zen_claw.auth.credentials import CredentialVault
    from zen_claw.config.schema import EmailConfig


def _decode_header(raw: str) -> str:
    parts = email.header.decode_header(raw or "")
    out = []
    for fragment, charset in parts:
        if isinstance(fragment, bytes):
            out.append(fragment.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(fragment)
    return "".join(out)


def _msg_to_summary(msg: email_lib.message.Message, uid: str, max_body: int = 200) -> str:
    subject = _decode_header(msg.get("Subject", "(no subject)"))
    from_ = _decode_header(msg.get("From", ""))
    date = msg.get("Date", "")
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    body = body.strip()[:max_body]
    return f"UID:{uid}  From:{from_}  Date:{date}\nSubject: {subject}\n{body}"


class EmailTool(Tool):
    """Read, send and search emails via IMAP/SMTP."""

    name = "email"
    description = (
        "Manage emails: read inbox, send email, search by keyword, get a specific email by UID, "
        "mark as read. Requires email tool to be enabled in config."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["read_inbox", "send", "search", "get", "mark_read"],
                "description": "Action to perform.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "description": "Max emails to return for read_inbox (default 10).",
            },
            "unread_only": {
                "type": "boolean",
                "description": "Only return unread messages for read_inbox.",
            },
            "to": {"type": "string", "description": "Recipient address (for send)."},
            "subject": {"type": "string", "description": "Email subject (for send)."},
            "body": {"type": "string", "description": "Email body text (for send)."},
            "query": {
                "type": "string",
                "description": "Search term: sender address, subject keyword, or date (YYYY-MM-DD).",
            },
            "uid": {"type": "string", "description": "Email UID (for get/mark_read)."},
        },
        "required": ["action"],
    }

    def __init__(self, config: "EmailConfig", vault: "CredentialVault | None" = None):
        self._cfg = config
        self._vault = vault

    def _get_password(self) -> str | None:
        if self._vault is None:
            from zen_claw.auth.credentials import CredentialVault
            self._vault = CredentialVault()
        return self._vault.get(self._cfg.credential_platform, self._cfg.credential_key)

    def _check_config(self) -> str | None:
        if not self._cfg.enabled:
            return "Email tool is not enabled. Set tools.email.enabled = true in config."
        if not self._cfg.imap_host or not self._cfg.username:
            return "Email tool requires imap_host and username in config."
        return None

    # ── IMAP helpers (sync, run in thread) ────────────────────────────────────

    def _imap_connect(self) -> imaplib.IMAP4 | imaplib.IMAP4_SSL:
        password = self._get_password() or ""
        if self._cfg.use_ssl:
            conn = imaplib.IMAP4_SSL(self._cfg.imap_host, self._cfg.imap_port)
        else:
            conn = imaplib.IMAP4(self._cfg.imap_host, self._cfg.imap_port)
        conn.login(self._cfg.username, password)
        return conn

    def _read_inbox(self, limit: int, unread_only: bool) -> str:
        conn = self._imap_connect()
        try:
            conn.select("INBOX")
            criteria = "(UNSEEN)" if unread_only else "ALL"
            _, data = conn.uid("search", None, criteria)
            uids = (data[0] or b"").split()
            uids = uids[-limit:]  # newest N
            if not uids:
                return "Inbox is empty." if not unread_only else "No unread messages."
            summaries = []
            for uid in reversed(uids):
                uid_str = uid.decode()
                _, msg_data = conn.uid("fetch", uid_str, "(RFC822)")
                if msg_data and msg_data[0]:
                    raw = msg_data[0][1]
                    if isinstance(raw, bytes):
                        msg = email_lib.message_from_bytes(raw)
                        summaries.append(_msg_to_summary(msg, uid_str))
            return "\n\n---\n\n".join(summaries) if summaries else "No messages found."
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def _search(self, query: str) -> str:
        conn = self._imap_connect()
        try:
            conn.select("INBOX")
            q = query.strip()
            # Date pattern YYYY-MM-DD
            import re
            if re.match(r"^\d{4}-\d{2}-\d{2}$", q):
                from datetime import datetime
                dt = datetime.strptime(q, "%Y-%m-%d")
                imap_date = dt.strftime("%d-%b-%Y")
                criteria = f'(SINCE "{imap_date}")'
            elif "@" in q:
                criteria = f'(FROM "{q}")'
            else:
                criteria = f'(SUBJECT "{q}")'
            _, data = conn.uid("search", None, criteria)
            uids = (data[0] or b"").split()
            uids = uids[-20:]
            if not uids:
                return f"No emails found matching: {query}"
            summaries = []
            for uid in reversed(uids):
                uid_str = uid.decode()
                _, msg_data = conn.uid("fetch", uid_str, "(RFC822)")
                if msg_data and msg_data[0]:
                    raw = msg_data[0][1]
                    if isinstance(raw, bytes):
                        msg = email_lib.message_from_bytes(raw)
                        summaries.append(_msg_to_summary(msg, uid_str))
            return "\n\n---\n\n".join(summaries) if summaries else "No messages found."
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def _get_by_uid(self, uid: str) -> str:
        conn = self._imap_connect()
        try:
            conn.select("INBOX")
            _, msg_data = conn.uid("fetch", uid, "(RFC822)")
            if not msg_data or not msg_data[0]:
                return f"No message found with UID {uid}."
            raw = msg_data[0][1]
            if not isinstance(raw, bytes):
                return f"No message found with UID {uid}."
            msg = email_lib.message_from_bytes(raw)
            return _msg_to_summary(msg, uid, max_body=2000)
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def _mark_read(self, uid: str) -> str:
        conn = self._imap_connect()
        try:
            conn.select("INBOX")
            conn.uid("store", uid, "+FLAGS", r"\Seen")
            return f"Marked UID {uid} as read."
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def _send(self, to: str, subject: str, body: str) -> str:
        password = self._get_password() or ""
        sender = self._cfg.sender_name or self._cfg.username
        msg = email.mime.multipart.MIMEMultipart("alternative")
        msg["From"] = f"{sender} <{self._cfg.username}>" if self._cfg.sender_name else self._cfg.username
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(email.mime.text.MIMEText(body, "plain", "utf-8"))
        if self._cfg.use_ssl:
            with smtplib.SMTP_SSL(self._cfg.smtp_host, self._cfg.smtp_port) as server:
                server.login(self._cfg.username, password)
                server.sendmail(self._cfg.username, [to], msg.as_string())
        else:
            with smtplib.SMTP(self._cfg.smtp_host, self._cfg.smtp_port) as server:
                server.ehlo()
                server.starttls()
                server.login(self._cfg.username, password)
                server.sendmail(self._cfg.username, [to], msg.as_string())
        return f"Email sent to {to}: {subject!r}"

    # ── Public execute ─────────────────────────────────────────────────────────

    async def execute(self, action: str, **kwargs: Any) -> ToolResult:
        err = self._check_config()
        if err:
            return ToolResult.failure(ToolErrorKind.PERMISSION, err, code="email_not_configured")

        try:
            if action == "read_inbox":
                limit = int(kwargs.get("limit") or 10)
                unread_only = bool(kwargs.get("unread_only", False))
                result = await asyncio.to_thread(self._read_inbox, limit, unread_only)
                return ToolResult.success(result)

            elif action == "send":
                to = str(kwargs.get("to") or "").strip()
                subject = str(kwargs.get("subject") or "").strip()
                body = str(kwargs.get("body") or "").strip()
                if not to:
                    return ToolResult.failure(ToolErrorKind.PARAMETER, "to is required", code="email_missing_to")
                if not self._cfg.smtp_host:
                    return ToolResult.failure(ToolErrorKind.PARAMETER, "smtp_host not configured", code="email_no_smtp")
                result = await asyncio.to_thread(self._send, to, subject, body)
                return ToolResult.success(result)

            elif action == "search":
                query = str(kwargs.get("query") or "").strip()
                if not query:
                    return ToolResult.failure(ToolErrorKind.PARAMETER, "query is required", code="email_missing_query")
                result = await asyncio.to_thread(self._search, query)
                return ToolResult.success(result)

            elif action == "get":
                uid = str(kwargs.get("uid") or "").strip()
                if not uid:
                    return ToolResult.failure(ToolErrorKind.PARAMETER, "uid is required", code="email_missing_uid")
                result = await asyncio.to_thread(self._get_by_uid, uid)
                return ToolResult.success(result)

            elif action == "mark_read":
                uid = str(kwargs.get("uid") or "").strip()
                if not uid:
                    return ToolResult.failure(ToolErrorKind.PARAMETER, "uid is required", code="email_missing_uid")
                result = await asyncio.to_thread(self._mark_read, uid)
                return ToolResult.success(result)

            else:
                return ToolResult.failure(
                    ToolErrorKind.PARAMETER,
                    f"Unknown action: {action!r}",
                    code="email_unknown_action",
                )
        except imaplib.IMAP4.error as exc:
            return ToolResult.failure(ToolErrorKind.RUNTIME, f"IMAP error: {exc}", code="email_imap_error")
        except smtplib.SMTPException as exc:
            return ToolResult.failure(ToolErrorKind.RUNTIME, f"SMTP error: {exc}", code="email_smtp_error")
        except Exception as exc:
            return ToolResult.failure(ToolErrorKind.RUNTIME, f"Email error: {exc}", code="email_error")

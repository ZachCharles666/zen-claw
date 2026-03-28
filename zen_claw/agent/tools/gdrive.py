"""Google Drive tool — list, read, upload and search files."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult

if TYPE_CHECKING:
    from zen_claw.config.schema import GDriveConfig

_GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
_GOOGLE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"
_GOOGLE_SLIDE_MIME = "application/vnd.google-apps.presentation"

_EXPORTABLE = {
    _GOOGLE_DOC_MIME: ("text/plain", ".txt"),
    _GOOGLE_SHEET_MIME: ("text/csv", ".csv"),
    _GOOGLE_SLIDE_MIME: ("text/plain", ".txt"),
}


def _load_google_credentials(token_file: str, credentials_file: str, scopes: list[str]) -> Any:
    """Load (or refresh) Google OAuth2 credentials. Returns google.oauth2.credentials.Credentials."""
    token_path = Path(token_file).expanduser()
    creds_path = Path(credentials_file).expanduser()

    try:
        from google.auth.transport.requests import Request  # type: ignore[import-untyped]
        from google.oauth2.credentials import Credentials  # type: ignore[import-untyped]
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]
    except ImportError:
        raise RuntimeError(
            "google-api-python-client / google-auth-oauthlib not installed. "
            "Run: pip install 'zen-claw-ai[calendar]'"
        )

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not creds_path.exists():
                raise RuntimeError(
                    f"Google credentials file not found: {creds_path}\n"
                    "Download it from Google Cloud Console → APIs & Services → Credentials."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), scopes)
            creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return creds


class GDriveTool(Tool):
    """Manage Google Drive: list, read, upload, search files and create folders."""

    name = "gdrive"
    description = (
        "Manage Google Drive files: list files in a folder, read text content (Docs/Sheets/plain text), "
        "upload a local file, search files by query, create a folder. "
        "Requires Google OAuth credentials configured."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list_files", "read_file", "upload_file", "search_files", "create_folder"],
                "description": "Action to perform.",
            },
            "file_id": {"type": "string", "description": "Google Drive file ID."},
            "folder_id": {
                "type": "string",
                "description": "Parent folder ID (default: root).",
            },
            "query": {
                "type": "string",
                "description": "Drive search query, e.g. 'name contains \"report\"' or 'mimeType=\"application/pdf\"'.",
            },
            "name": {"type": "string", "description": "File or folder name."},
            "local_path": {
                "type": "string",
                "description": "Absolute local file path to upload.",
            },
            "mime_type": {
                "type": "string",
                "description": "MIME type for upload (auto-detected if omitted).",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "Max files to return (default 20).",
            },
        },
        "required": ["action"],
    }

    def __init__(self, config: "GDriveConfig"):
        self._cfg = config
        self._service: Any = None

    def _get_service(self) -> Any:
        if self._service is None:
            creds = _load_google_credentials(
                self._cfg.token_file,
                self._cfg.credentials_file,
                self._cfg.scopes,
            )
            try:
                from googleapiclient.discovery import build  # type: ignore[import-untyped]
            except ImportError:
                raise RuntimeError(
                    "google-api-python-client not installed. Run: pip install 'zen-claw-ai[calendar]'"
                )
            self._service = build("drive", "v3", credentials=creds)
        return self._service

    def _check_config(self) -> str | None:
        if not self._cfg.enabled:
            return "Google Drive tool is not enabled. Set tools.gdrive.enabled = true in config."
        return None

    # ── Sync operations ────────────────────────────────────────────────────────

    def _list_files(self, folder_id: str | None, limit: int) -> str:
        service = self._get_service()
        q = f"'{folder_id}' in parents and trashed=false" if folder_id else "trashed=false"
        resp = (
            service.files()
            .list(
                q=q,
                pageSize=limit,
                fields="files(id, name, mimeType, modifiedTime, size)",
                orderBy="modifiedTime desc",
            )
            .execute()
        )
        files = resp.get("files", [])
        if not files:
            return "No files found."
        lines = []
        for f in files:
            size = f.get("size", "—")
            lines.append(
                f"{f['id']}  {f['mimeType'][:40]:<42}  {f.get('modifiedTime','')[:10]}  {size:>10}  {f['name']}"
            )
        return "id  mimeType  modified  size  name\n" + "\n".join(lines)

    def _read_file(self, file_id: str) -> str:
        service = self._get_service()
        meta = service.files().get(fileId=file_id, fields="name,mimeType").execute()
        mime = meta.get("mimeType", "")
        name = meta.get("name", file_id)

        if mime in _EXPORTABLE:
            export_mime, _ = _EXPORTABLE[mime]
            data = service.files().export(fileId=file_id, mimeType=export_mime).execute()
            if isinstance(data, bytes):
                text = data.decode("utf-8", errors="replace")
            else:
                text = str(data)
            return f"[{name}]\n\n{text[:8000]}"

        if mime.startswith("text/"):
            buf = io.BytesIO()
            request = service.files().get_media(fileId=file_id)
            from googleapiclient.http import MediaIoBaseDownload  # type: ignore[import-untyped]
            dl = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = dl.next_chunk()
            text = buf.getvalue().decode("utf-8", errors="replace")
            return f"[{name}]\n\n{text[:8000]}"

        return f"[{name}] is a binary file ({mime}) and cannot be read as text."

    def _upload_file(self, local_path: str, folder_id: str | None, mime_type: str | None) -> str:
        import mimetypes

        service = self._get_service()
        path = Path(local_path)
        if not path.exists():
            raise FileNotFoundError(f"Local file not found: {local_path}")

        detected_mime, _ = mimetypes.guess_type(str(path))
        effective_mime = mime_type or detected_mime or "application/octet-stream"

        from googleapiclient.http import MediaFileUpload  # type: ignore[import-untyped]

        meta: dict[str, Any] = {"name": path.name}
        if folder_id:
            meta["parents"] = [folder_id]

        media = MediaFileUpload(str(path), mimetype=effective_mime, resumable=False)
        result = service.files().create(body=meta, media_body=media, fields="id,name").execute()
        return f"Uploaded '{path.name}' → id={result['id']}"

    def _search_files(self, query: str, limit: int) -> str:
        service = self._get_service()
        safe_q = f"({query}) and trashed=false"
        resp = (
            service.files()
            .list(
                q=safe_q,
                pageSize=limit,
                fields="files(id, name, mimeType, modifiedTime)",
                orderBy="modifiedTime desc",
            )
            .execute()
        )
        files = resp.get("files", [])
        if not files:
            return f"No files found matching: {query!r}"
        lines = [f"{f['id']}  {f.get('modifiedTime','')[:10]}  {f['name']}" for f in files]
        return "\n".join(lines)

    def _create_folder(self, name: str, parent_id: str | None) -> str:
        service = self._get_service()
        meta: dict[str, Any] = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        if parent_id:
            meta["parents"] = [parent_id]
        result = service.files().create(body=meta, fields="id,name").execute()
        return f"Created folder '{name}' → id={result['id']}"

    # ── Public execute ─────────────────────────────────────────────────────────

    async def execute(self, action: str, **kwargs: Any) -> ToolResult:
        err = self._check_config()
        if err:
            return ToolResult.failure(ToolErrorKind.PERMISSION, err, code="gdrive_not_configured")

        try:
            limit = int(kwargs.get("limit") or 20)

            if action == "list_files":
                folder_id = kwargs.get("folder_id") or None
                result = await asyncio.to_thread(self._list_files, folder_id, limit)
                return ToolResult.success(result)

            elif action == "read_file":
                file_id = str(kwargs.get("file_id") or "").strip()
                if not file_id:
                    return ToolResult.failure(
                        ToolErrorKind.PARAMETER, "file_id is required", code="gdrive_missing_file_id"
                    )
                result = await asyncio.to_thread(self._read_file, file_id)
                return ToolResult.success(result)

            elif action == "upload_file":
                local_path = str(kwargs.get("local_path") or "").strip()
                if not local_path:
                    return ToolResult.failure(
                        ToolErrorKind.PARAMETER, "local_path is required", code="gdrive_missing_path"
                    )
                folder_id = kwargs.get("folder_id") or None
                mime_type = kwargs.get("mime_type") or None
                result = await asyncio.to_thread(self._upload_file, local_path, folder_id, mime_type)
                return ToolResult.success(result)

            elif action == "search_files":
                query = str(kwargs.get("query") or "").strip()
                if not query:
                    return ToolResult.failure(
                        ToolErrorKind.PARAMETER, "query is required", code="gdrive_missing_query"
                    )
                result = await asyncio.to_thread(self._search_files, query, limit)
                return ToolResult.success(result)

            elif action == "create_folder":
                name = str(kwargs.get("name") or "").strip()
                if not name:
                    return ToolResult.failure(
                        ToolErrorKind.PARAMETER, "name is required", code="gdrive_missing_name"
                    )
                folder_id = kwargs.get("folder_id") or None
                result = await asyncio.to_thread(self._create_folder, name, folder_id)
                return ToolResult.success(result)

            else:
                return ToolResult.failure(
                    ToolErrorKind.PARAMETER,
                    f"Unknown action: {action!r}",
                    code="gdrive_unknown_action",
                )

        except FileNotFoundError as exc:
            return ToolResult.failure(ToolErrorKind.PARAMETER, str(exc), code="gdrive_file_not_found")
        except RuntimeError as exc:
            return ToolResult.failure(ToolErrorKind.PERMISSION, str(exc), code="gdrive_setup_error")
        except Exception as exc:
            return ToolResult.failure(ToolErrorKind.RUNTIME, f"Drive error: {exc}", code="gdrive_error")

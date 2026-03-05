"""File system tools: read, write, edit."""

from pathlib import Path
from typing import Any

from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult


def _resolve_path(path: str, allowed_dir: Path | None = None) -> Path:
    """Resolve path and optionally enforce directory restriction."""
    resolved = Path(path).expanduser().resolve()
    if allowed_dir:
        allowed_root = allowed_dir.expanduser().resolve()
        try:
            resolved.relative_to(allowed_root)
        except ValueError:
            raise PermissionError(f"Path {path} is outside allowed directory {allowed_dir}")
    return resolved


class ReadFileTool(Tool):
    """Tool to read file contents."""

    def __init__(self, allowed_dir: Path | None = None):
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "Read the contents of a file at the given path."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "The file path to read"}},
            "required": ["path"],
        }

    async def execute(self, path: str, **kwargs: Any) -> ToolResult:
        try:
            file_path = _resolve_path(path, self._allowed_dir)
            if not file_path.exists():
                return ToolResult.failure(
                    ToolErrorKind.PARAMETER,
                    f"File not found: {path}",
                    code="file_not_found",
                )
            if not file_path.is_file():
                return ToolResult.failure(
                    ToolErrorKind.PARAMETER,
                    f"Not a file: {path}",
                    code="not_a_file",
                )

            content = file_path.read_text(encoding="utf-8")
            return ToolResult.success(content)
        except PermissionError as e:
            return ToolResult.failure(
                ToolErrorKind.PERMISSION,
                str(e),
                code="path_outside_workspace",
            )
        except Exception as e:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                f"Error reading file: {str(e)}",
                code="read_file_failed",
            )


class WriteFileTool(Tool):
    """Tool to write content to a file."""

    def __init__(self, allowed_dir: Path | None = None):
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "Write content to a file at the given path. Creates parent directories if needed."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The file path to write to"},
                "content": {"type": "string", "description": "The content to write"},
            },
            "required": ["path", "content"],
        }

    async def execute(self, path: str, content: str, **kwargs: Any) -> ToolResult:
        try:
            file_path = _resolve_path(path, self._allowed_dir)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            return ToolResult.success(
                f"Successfully wrote {len(content)} bytes to {path}",
                bytes_written=len(content),
            )
        except PermissionError as e:
            return ToolResult.failure(
                ToolErrorKind.PERMISSION,
                str(e),
                code="path_outside_workspace",
            )
        except Exception as e:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                f"Error writing file: {str(e)}",
                code="write_file_failed",
            )


class EditFileTool(Tool):
    """Tool to edit a file by replacing text."""

    def __init__(self, allowed_dir: Path | None = None):
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return "Edit a file by replacing old_text with new_text. The old_text must exist exactly in the file."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The file path to edit"},
                "old_text": {"type": "string", "description": "The exact text to find and replace"},
                "new_text": {"type": "string", "description": "The text to replace with"},
            },
            "required": ["path", "old_text", "new_text"],
        }

    async def execute(self, path: str, old_text: str, new_text: str, **kwargs: Any) -> ToolResult:
        try:
            file_path = _resolve_path(path, self._allowed_dir)
            if not file_path.exists():
                return ToolResult.failure(
                    ToolErrorKind.PARAMETER,
                    f"File not found: {path}",
                    code="file_not_found",
                )

            content = file_path.read_text(encoding="utf-8")

            if old_text not in content:
                return ToolResult.failure(
                    ToolErrorKind.PARAMETER,
                    "old_text not found in file. Make sure it matches exactly.",
                    code="old_text_not_found",
                )

            # Count occurrences
            count = content.count(old_text)
            if count > 1:
                return ToolResult.failure(
                    ToolErrorKind.PARAMETER,
                    f"old_text appears {count} times. Please provide more context to make it unique.",
                    code="old_text_not_unique",
                )

            new_content = content.replace(old_text, new_text, 1)
            file_path.write_text(new_content, encoding="utf-8")

            return ToolResult.success(f"Successfully edited {path}")
        except PermissionError as e:
            return ToolResult.failure(
                ToolErrorKind.PERMISSION,
                str(e),
                code="path_outside_workspace",
            )
        except Exception as e:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                f"Error editing file: {str(e)}",
                code="edit_file_failed",
            )


class ListDirTool(Tool):
    """Tool to list directory contents."""

    def __init__(self, allowed_dir: Path | None = None):
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "list_dir"

    @property
    def description(self) -> str:
        return "List the contents of a directory."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "The directory path to list"}},
            "required": ["path"],
        }

    async def execute(self, path: str, **kwargs: Any) -> ToolResult:
        try:
            dir_path = _resolve_path(path, self._allowed_dir)
            if not dir_path.exists():
                return ToolResult.failure(
                    ToolErrorKind.PARAMETER,
                    f"Directory not found: {path}",
                    code="directory_not_found",
                )
            if not dir_path.is_dir():
                return ToolResult.failure(
                    ToolErrorKind.PARAMETER,
                    f"Not a directory: {path}",
                    code="not_a_directory",
                )

            items = []
            for item in sorted(dir_path.iterdir()):
                prefix = "📁 " if item.is_dir() else "📄 "
                items.append(f"{prefix}{item.name}")

            if not items:
                return ToolResult.success(f"Directory {path} is empty")

            return ToolResult.success("\n".join(items))
        except PermissionError as e:
            return ToolResult.failure(
                ToolErrorKind.PERMISSION,
                str(e),
                code="path_outside_workspace",
            )
        except Exception as e:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                f"Error listing directory: {str(e)}",
                code="list_dir_failed",
            )

"""Utility functions for zen_claw."""

from datetime import datetime
from pathlib import Path
from tempfile import gettempdir


def ensure_dir(path: Path) -> Path:
    """Ensure a directory exists, creating it if necessary."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_data_path() -> Path:
    """Get the zen-claw data directory (~/.zen-claw)."""
    return ensure_dir(Path.home() / ".zen-claw")


def get_workspace_path(workspace: str | None = None) -> Path:
    """
    Get the workspace path.

    Args:
        workspace: Optional workspace path. Defaults to ~/.zen-claw/workspace.

    Returns:
        Expanded and ensured workspace path.
    """
    if workspace:
        path = Path(workspace).expanduser()
    else:
        path = Path.home() / ".zen-claw" / "workspace"
    return ensure_dir(path)


def get_sessions_path(workspace: Path | None = None) -> Path:
    """Get the sessions storage directory with a writable fallback chain."""
    candidates: list[Path] = []
    if workspace is not None:
        candidates.append(Path(workspace) / ".zen-claw" / "sessions")
    candidates.append(Path.cwd() / ".zen-claw" / "sessions")
    candidates.append(get_data_path() / "sessions")
    candidates.append(Path(gettempdir()) / "zen-claw" / "sessions")

    last_error: Exception | None = None
    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            return ensure_dir(candidate)
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise RuntimeError("failed to resolve sessions directory")


def get_memory_path(workspace: Path | None = None) -> Path:
    """Get the memory directory within the workspace."""
    ws = workspace or get_workspace_path()
    return ensure_dir(ws / "memory")


def get_skills_path(workspace: Path | None = None) -> Path:
    """Get the skills directory within the workspace."""
    ws = workspace or get_workspace_path()
    return ensure_dir(ws / "skills")


def today_date() -> str:
    """Get today's date in YYYY-MM-DD format."""
    return datetime.now().strftime("%Y-%m-%d")


def timestamp() -> str:
    """Get current timestamp in ISO format."""
    return datetime.now().isoformat()


def truncate_string(s: str, max_len: int = 100, suffix: str = "...") -> str:
    """Truncate a string to max length, adding suffix if truncated."""
    if len(s) <= max_len:
        return s
    return s[: max_len - len(suffix)] + suffix


def safe_filename(name: str) -> str:
    """Convert a string to a safe filename."""
    # Replace unsafe characters
    unsafe = '<>:"/\\|?*'
    for char in unsafe:
        name = name.replace(char, "_")
    return name.strip()


def parse_session_key(key: str) -> tuple[str, str]:
    """
    Parse a session key into channel and chat_id.

    Args:
        key: Session key in format "channel:chat_id"

    Returns:
        Tuple of (channel, chat_id)
    """
    parts = key.split(":", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid session key: {key}")
    return parts[0], parts[1]

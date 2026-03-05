"""zen-claw package metadata."""

import tomllib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as dist_version
from pathlib import Path


def _read_pyproject_version() -> str | None:
    """Best-effort fallback for source checkout mode (not installed)."""
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not pyproject.exists():
        return None

    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except Exception:
        return None

    project = data.get("project", {})
    version = project.get("version")
    return version if isinstance(version, str) and version else None


def _resolve_version() -> str:
    """Resolve package version from installed metadata, with local fallback."""
    try:
        return dist_version("zen-claw-ai")
    except PackageNotFoundError:
        fallback = _read_pyproject_version()
        if fallback:
            return fallback
        return "0.0.0"


__version__ = _resolve_version()
__logo__ = "🧘"



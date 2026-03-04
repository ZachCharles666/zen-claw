from pathlib import Path

import pytest


def test_exec_sidecar_release_checklist_exists_and_has_sections() -> None:
    p = Path("workspace/EXEC_SIDECAR_RELEASE_CHECKLIST.md")
    if not p.exists():
        pytest.skip("EXEC_SIDECAR_RELEASE_CHECKLIST.md not present in this repository snapshot")
    text = p.read_text(encoding="utf-8")
    assert "# Exec Sidecar Release Checklist (P2 Entry)" in text
    assert "## Scope" in text
    assert "## Release Entry Criteria" in text
    assert "## Verification Commands" in text

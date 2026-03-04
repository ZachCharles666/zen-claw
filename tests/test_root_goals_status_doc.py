from pathlib import Path

import pytest


def test_root_goals_status_has_required_sections() -> None:
    p = Path("workspace/ROOT_GOALS_STATUS.md")
    if not p.exists():
        pytest.skip("ROOT_GOALS_STATUS.md not present in this repository snapshot")
    text = p.read_text(encoding="utf-8")
    assert "# Root Goals Status" in text
    assert "## Goal 1: Capability close to OpenClaw" in text
    assert "## Goal 2: Code size significantly smaller than OpenClaw" in text
    assert "## Goal 3: Lightweight, readable, maintainable, fast iteration" in text
    assert "## Next Iteration Priorities" in text
    assert "## Update Rules" in text

from pathlib import Path

import pytest


def test_openclaw_coverage_contains_snapshot_and_gaps() -> None:
    p = Path("OPENCLAW_COVERAGE.md")
    if not p.exists():
        pytest.skip("OPENCLAW_COVERAGE.md not present in this repository snapshot")
    text = p.read_text(encoding="utf-8")
    assert "# OpenClaw Coverage Matrix" in text
    assert "## Capability Coverage" in text
    assert "## Coverage Snapshot" in text
    assert "## Highest-Priority Gaps" in text

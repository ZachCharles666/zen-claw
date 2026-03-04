from pathlib import Path


def test_ci_workflow_includes_loc_gate() -> None:
    p = Path(".github/workflows/ci.yml")
    assert p.exists(), "ci workflow should exist"
    text = p.read_text(encoding="utf-8")
    assert "loc-gate:" in text
    assert "scripts/loc_report.ps1" in text
    assert "workspace\\LOC_BASELINE.json" in text

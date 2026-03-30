from pathlib import Path


def test_ci_workflow_includes_loc_gate() -> None:
    p = Path(".github/workflows/ci.yml")
    assert p.exists(), "ci workflow should exist"
    text = p.read_text(encoding="utf-8")
    assert "lint-and-guards:" in text
    assert "python -m ruff check ." in text
    assert "scripts/loc_report.ps1" in text
    assert "workspace\\LOC_BASELINE.json" in text
    assert "scripts/run_ci_suite.py" in text
    assert "shell: python" not in text

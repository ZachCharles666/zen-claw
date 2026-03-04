from pathlib import Path


def test_nightly_integration_workflow_exists_and_runs_selftest() -> None:
    p = Path(".github/workflows/nightly-integration.yml")
    assert p.exists(), "nightly integration workflow should exist"
    text = p.read_text(encoding="utf-8")
    assert "schedule:" in text
    assert "workflow_dispatch:" in text
    assert "runs-on: windows-latest" in text
    assert "scripts/selftest_local.ps1" in text
    assert "scripts/perf_baseline.ps1" in text
    assert "scripts/perf_report.ps1" in text
    assert "PERFORMANCE_REPORT.md" in text

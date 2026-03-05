import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="powershell-based test for Windows")
def test_perf_report_script_renders_markdown(tmp_path: Path) -> None:
    inp = tmp_path / "perf.json"
    inp.write_text(
        (
            "{"
            '"generated_at":"2026-02-17T00:00:00Z",'
            '"repo_root":"E:/zen-claw",'
            '"metrics_ms":{'
            '"startup_import":12.3,'
            '"tool_list_dir":45.6,'
            '"pytest_smoke":78.9'
            "}"
            "}"
        ),
        encoding="utf-8",
    )
    out = tmp_path / "PERFORMANCE_REPORT.md"
    repo_root = Path(__file__).resolve().parents[1]
    cmd = [
        "powershell",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(repo_root / "scripts" / "perf_report.ps1"),
        "-InputFile",
        str(inp),
        "-OutFile",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out.exists()
    text = out.read_text(encoding="utf-8-sig")
    assert "# Performance Report" in text
    assert "startup_import" in text

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="powershell-based test for Windows")
def test_loc_report_fail_on_increase_blocks_when_over_threshold(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "generated_at": "2026-02-14T00:00:00Z",
                "repo_root": str(repo_root),
                "python": {"files": 1, "nonblank_lines": 1},
                "go": {"files": 0, "nonblank_lines": 0},
                "total_nonblank_lines": 1,
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out.json"
    cmd = [
        "powershell",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(repo_root / "scripts" / "loc_report.ps1"),
        "-RepoRoot",
        str(repo_root),
        "-OutFile",
        str(out),
        "-FailOnIncrease",
        "-BaselineFile",
        str(baseline),
        "-MaxIncreasePercent",
        "0",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=os.environ.copy())
    assert proc.returncode != 0
    assert "LOC increased by" in (proc.stdout + proc.stderr)


@pytest.mark.skipif(sys.platform != "win32", reason="powershell-based test for Windows")
def test_loc_report_fail_on_increase_allows_within_threshold(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    probe_out = tmp_path / "probe.json"
    probe_cmd = [
        "powershell",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(repo_root / "scripts" / "loc_report.ps1"),
        "-RepoRoot",
        str(repo_root),
        "-OutFile",
        str(probe_out),
    ]
    probe = subprocess.run(probe_cmd, capture_output=True, text=True, env=os.environ.copy())
    assert probe.returncode == 0
    current_total = int(
        json.loads(probe_out.read_text(encoding="utf-8-sig"))["total_nonblank_lines"]
    )

    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "generated_at": "2026-02-14T00:00:00Z",
                "repo_root": str(repo_root),
                "python": {"files": 1, "nonblank_lines": 1},
                "go": {"files": 1, "nonblank_lines": 1},
                "total_nonblank_lines": current_total,
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out.json"
    cmd = [
        "powershell",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(repo_root / "scripts" / "loc_report.ps1"),
        "-RepoRoot",
        str(repo_root),
        "-OutFile",
        str(out),
        "-FailOnIncrease",
        "-BaselineFile",
        str(baseline),
        "-MaxIncreasePercent",
        "0",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=os.environ.copy())
    assert proc.returncode == 0
    assert out.exists()

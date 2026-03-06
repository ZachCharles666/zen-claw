import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _make_mini_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "mini-repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "pyproject.toml").write_text("[project]\nname='mini'\nversion='0.0.0'\n", encoding="utf-8")
    src = repo_root / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "app.py").write_text("print('hello')\n\nx = 1\n", encoding="utf-8")
    (src / "worker.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    go_dir = repo_root / "go"
    go_dir.mkdir(parents=True, exist_ok=True)
    (go_dir / "main.go").write_text("package main\n\nfunc main() {}\n", encoding="utf-8")
    (go_dir / "helper.go").write_text("package main\n\nfunc helper() int { return 1 }\n", encoding="utf-8")
    return repo_root


@pytest.mark.skipif(sys.platform != "win32", reason="powershell-based test for Windows")
def test_loc_report_fail_on_increase_blocks_when_over_threshold(tmp_path: Path) -> None:
    repo_root = _make_mini_repo(tmp_path)
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "loc_report.ps1"
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
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
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
    repo_root = _make_mini_repo(tmp_path)
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "loc_report.ps1"
    probe_out = tmp_path / "probe.json"
    probe_cmd = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
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
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
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

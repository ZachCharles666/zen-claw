import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="powershell-based test for Windows")
def test_perf_baseline_script_generates_metrics(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    out = tmp_path / "perf.json"
    cmd = [
        "powershell",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(repo_root / "scripts" / "perf_baseline.ps1"),
        "-RepoRoot",
        str(repo_root),
        "-PythonExe",
        "python",
        "-OutFile",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8-sig"))
    assert "metrics_ms" in data
    for key in ("startup_import", "tool_list_dir", "pytest_smoke"):
        assert key in data["metrics_ms"]

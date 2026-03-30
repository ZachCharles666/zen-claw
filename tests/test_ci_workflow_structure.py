from pathlib import Path


def test_ci_workflow_uses_repo_scripted_suites() -> None:
    p = Path(".github/workflows/ci.yml")
    assert p.exists(), "ci workflow should exist"
    text = p.read_text(encoding="utf-8")
    assert "lint-and-guards:" in text
    assert "core-tests:" in text
    assert "memory-recall:" in text
    assert "channel-matrix:" in text
    assert "summary:" in text
    assert "scripts/run_ci_suite.py --suite core --total-shards 4 --shard-index ${{ matrix.shard }}" in text
    assert "scripts/run_ci_suite.py --suite memory-recall" in text
    assert "scripts/run_ci_suite.py --suite ${{ matrix.suite.name }}" in text
    assert "ConvertFrom-Json" not in text
    assert "Path(\"tests\").glob(\"test_*.py\")" not in text


def test_preflight_script_exists() -> None:
    p = Path("scripts/preflight.ps1")
    assert p.exists(), "preflight script should exist"
    text = p.read_text(encoding="utf-8")
    assert "[ValidateSet(\"quick\", \"full\", \"nightly\")]" in text
    assert "scripts\\ci_select_tests.py" in text
    assert "--basetemp" in text
    assert ".pytest_tmp" in text
    assert "loc_report.ps1" in text


def test_ci_suite_runner_exists() -> None:
    p = Path("scripts/run_ci_suite.py")
    assert p.exists(), "ci suite runner should exist"
    text = p.read_text(encoding="utf-8")
    assert "resolve_suite_tests" in text
    assert "subprocess.run" in text
    assert "--basetemp" in text

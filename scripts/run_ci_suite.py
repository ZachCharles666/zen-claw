from __future__ import annotations

import argparse
import os
import tempfile
import uuid
from pathlib import Path

import pytest
from ci_select_tests import resolve_suite_tests, shard_tests


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a stable CI pytest suite from repo-owned logic.")
    parser.add_argument("--suite", required=True)
    parser.add_argument("--total-shards", type=int, default=None)
    parser.add_argument("--shard-index", type=int, default=None)
    parser.add_argument("--timeout-seconds", type=int, default=None)
    parser.add_argument("--basetemp-root", default=None)
    return parser


def resolve_basetemp_root(explicit_root: str | None) -> Path:
    if explicit_root:
        return Path(explicit_root)
    runner_temp = os.environ.get("RUNNER_TEMP")
    if runner_temp:
        return Path(runner_temp) / "zen-claw-pytest"
    return Path(tempfile.gettempdir()) / "zen-claw-pytest"


def build_basetemp_path(suite: str, shard_index: int | None, basetemp_root: Path) -> Path:
    safe_suite = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in suite)
    run_token = uuid.uuid4().hex[:8]
    if shard_index is not None:
        leaf = f"{safe_suite}-shard-{shard_index}-{run_token}"
    else:
        leaf = f"{safe_suite}-{run_token}"
    return basetemp_root / leaf


def main() -> int:
    args = build_parser().parse_args()
    selected_tests = shard_tests(
        suite=args.suite,
        tests=resolve_suite_tests(args.suite),
        total_shards=args.total_shards,
        shard_index=args.shard_index,
    )

    basetemp_root = resolve_basetemp_root(args.basetemp_root)
    basetemp = build_basetemp_path(args.suite, args.shard_index, basetemp_root)
    basetemp.parent.mkdir(parents=True, exist_ok=True)

    if args.shard_index is None or args.total_shards is None:
        print(f"Running suite {args.suite}: {' '.join(selected_tests)}")
    else:
        print(
            f"Running {args.suite} shard {args.shard_index}/{args.total_shards}: "
            + " ".join(selected_tests)
        )

    pytest_args = [
        "-p",
        "no:timeout",
        "--basetemp",
        str(basetemp),
        "-q",
        *selected_tests,
    ]
    if args.timeout_seconds is not None:
        print(
            "NOTE: --timeout-seconds is accepted for workflow compatibility; "
            "job-level timeout remains authoritative in the in-process runner."
        )
    exit_code = pytest.main(pytest_args)
    print(f"Pytest exit code: {exit_code}")
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())

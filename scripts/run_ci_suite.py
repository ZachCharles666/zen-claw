from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

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
    leaf = f"{safe_suite}-shard-{shard_index}" if shard_index is not None else safe_suite
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

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:timeout",
        "--basetemp",
        str(basetemp),
        "-q",
        *selected_tests,
    ]
    try:
        proc = subprocess.run(cmd, check=False, timeout=args.timeout_seconds)
    except subprocess.TimeoutExpired:
        print(f"ERROR: pytest timed out after {args.timeout_seconds} seconds")
        return 1
    print(f"Pytest exit code: {proc.returncode}")
    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())

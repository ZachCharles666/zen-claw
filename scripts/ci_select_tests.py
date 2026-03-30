from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Final

TESTS_DIR: Final[Path] = Path("tests")

SUITE_TESTS: Final[dict[str, list[str]]] = {
    "memory-recall": ["tests/test_memory_recall.py"],
    "core-runtime-sensitive": [
        "tests/test_production_hardening_config.py",
        "tests/test_rag_notebook_mgmt.py",
        "tests/test_release_gate_defaults.py",
        "tests/test_semantic_tool_selector.py",
        "tests/test_sidecar_supervisor_status.py",
        "tests/test_skill_scope_runtime_gate.py",
        "tests/test_skills_lifecycle.py",
    ],
    "channel-webchat-webhook": [
        "tests/test_webchat_channel.py",
        "tests/test_webhook_trigger.py",
    ],
    "channel-slack": ["tests/test_slack_channel.py"],
    "channel-signal": ["tests/test_signal_channel.py"],
    "channel-matrix": ["tests/test_matrix_channel.py"],
}

CORE_EXCLUDE: Final[set[str]] = {
    "tests/test_webchat_channel.py",
    "tests/test_webhook_trigger.py",
    "tests/test_slack_channel.py",
    "tests/test_signal_channel.py",
    "tests/test_matrix_channel.py",
    "tests/test_memory_recall.py",
    *SUITE_TESTS["core-runtime-sensitive"],
}


def get_core_tests() -> list[str]:
    return sorted(
        f"tests/{path.name}"
        for path in TESTS_DIR.glob("test_*.py")
        if f"tests/{path.name}" not in CORE_EXCLUDE
    )


def resolve_suite_tests(name: str) -> list[str]:
    if name == "core":
        return get_core_tests()
    try:
        return list(SUITE_TESTS[name])
    except KeyError as exc:
        valid = ", ".join(sorted(["core", *SUITE_TESTS]))
        raise SystemExit(f"Unknown suite '{name}'. Valid suites: {valid}") from exc


def shard_tests(
    suite: str,
    tests: list[str],
    total_shards: int | None,
    shard_index: int | None,
) -> list[str]:
    if total_shards is None and shard_index is None:
        return tests
    if total_shards is None or shard_index is None:
        raise SystemExit("Both --total-shards and --shard-index must be provided together.")
    if total_shards < 1:
        raise SystemExit(f"--total-shards must be >= 1, got {total_shards}.")
    if shard_index < 0 or shard_index >= total_shards:
        raise SystemExit(
            f"--shard-index must be between 0 and {total_shards - 1}, got {shard_index}."
        )
    selected = [test for idx, test in enumerate(tests) if idx % total_shards == shard_index]
    if not selected:
        raise SystemExit(
            "No tests selected for suite="
            f"{suite!r}, shard_index={shard_index}, total_shards={total_shards}."
        )
    return selected


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resolve stable CI test suites and shard selection.")
    parser.add_argument("--suite", required=True, help="Suite name: core, memory-recall, channel-*")
    parser.add_argument(
        "--format",
        choices=("args", "json"),
        default="args",
        help="Output either space-delimited pytest args or a JSON object.",
    )
    parser.add_argument("--total-shards", type=int, default=None)
    parser.add_argument("--shard-index", type=int, default=None)
    return parser


def render_json_payload(
    suite: str,
    tests: list[str],
    total_shards: int | None,
    shard_index: int | None,
) -> str:
    payload = {
        "suite": suite,
        "tests": tests,
        "total_shards": total_shards,
        "shard_index": shard_index,
    }
    return json.dumps(payload)


if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()
    selected_tests = shard_tests(
        suite=args.suite,
        tests=resolve_suite_tests(args.suite),
        total_shards=args.total_shards,
        shard_index=args.shard_index,
    )
    if args.format == "json":
        print(
            render_json_payload(
                suite=args.suite,
                tests=selected_tests,
                total_shards=args.total_shards,
                shard_index=args.shard_index,
            )
        )
    else:
        print(" ".join(selected_tests))

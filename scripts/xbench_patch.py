"""Patch xbench-evals/language_models.py to add zen-claw as an evaluated model.

Usage (run from inside the cloned xbench-evals repo):
    python <zen-claw-repo>/scripts/xbench_patch.py [--base-url http://127.0.0.1:18791/v1]

The script is idempotent — safe to run multiple times.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def _patch(lm_path: Path, base_url: str) -> bool:
    """Apply zen-claw entries to language_models.py.

    Returns True if any change was made, False if already patched.
    """
    src = lm_path.read_text(encoding="utf-8")
    changed = False

    # ── 1. API_ENDPOINTS ────────────────────────────────────────────────────
    endpoint_marker = '"zen-claw"'
    if endpoint_marker not in src:
        # Insert before the closing brace of API_ENDPOINTS dict
        pattern = r"(API_ENDPOINTS\s*=\s*\{[^}]*?)\n(\})"
        replacement = rf'\1\n    "zen-claw": "{base_url}",\n\2'
        new_src = re.sub(pattern, replacement, src, count=1, flags=re.DOTALL)
        if new_src != src:
            src = new_src
            changed = True
            print(f"[+] Added 'zen-claw' to API_ENDPOINTS → {base_url}")
        else:
            print("[!] Could not locate API_ENDPOINTS dict — add manually:")
            print(f'    "zen-claw": "{base_url}",')
    else:
        print("[=] 'zen-claw' already in API_ENDPOINTS")

    # ── 2. API_PRICE ─────────────────────────────────────────────────────────
    price_marker = '"zen-claw"'
    if "API_PRICE" in src and price_marker not in src.split("API_PRICE")[1][:500]:
        pattern = r"(API_PRICE\s*=\s*\{[^}]*?)\n(\})"
        replacement = r'\1\n    "zen-claw": (0.0, 0.0, 1.0),\n\2'
        new_src = re.sub(pattern, replacement, src, count=1, flags=re.DOTALL)
        if new_src != src:
            src = new_src
            changed = True
            print("[+] Added 'zen-claw' to API_PRICE (0 cost)")
        else:
            print("[!] Could not locate API_PRICE dict — add manually:")
            print('    "zen-claw": (0.0, 0.0, 1.0),')
    else:
        print("[=] 'zen-claw' already in API_PRICE (or API_PRICE not found)")

    # ── 3. get_llm_response match/case ───────────────────────────────────────
    case_marker = 'case "zen-claw"'
    if case_marker not in src:
        # Insert before the wildcard case _ or the last case
        zen_case = (
            '        case "zen-claw":\n'
            "            response = get_api_response(\n"
            "                prompt,\n"
            '                model="zen-claw",\n'
            '                api_key=os.getenv("ZEN_CLAW_API_KEY", "zen-claw"),\n'
            f'                base_url="{base_url}",\n'
            "            )\n"
        )
        # Try to insert before the fallback `case _:` block
        pattern = r"(\n        case _:)"
        new_src = re.sub(pattern, r"\n" + zen_case + r"\1", src, count=1)
        if new_src != src:
            src = new_src
            changed = True
            print("[+] Added 'zen-claw' case to get_llm_response()")
        else:
            print("[!] Could not locate match statement — add manually before 'case _:':")
            print(zen_case)
    else:
        print("[=] 'zen-claw' case already in get_llm_response()")

    if changed:
        lm_path.write_text(src, encoding="utf-8")
        print(f"\n✓ Patched {lm_path}")
    else:
        print("\n✓ No changes needed — already patched")

    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description="Patch xbench-evals with zen-claw model")
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:18791/v1",
        help="zen-claw OpenAI-compat base URL (default: http://127.0.0.1:18791/v1)",
    )
    parser.add_argument(
        "--lm-file",
        default="language_models.py",
        help="Path to language_models.py (default: ./language_models.py)",
    )
    args = parser.parse_args()

    lm_path = Path(args.lm_file)
    if not lm_path.exists():
        print(f"[ERROR] {lm_path} not found.")
        print("Run this script from inside the xbench-evals directory:")
        print("  cd xbench-evals")
        print("  python ../zen-claw/scripts/xbench_patch.py")
        sys.exit(1)

    print(f"Patching {lm_path} to add zen-claw → {args.base_url}\n")
    _patch(lm_path, args.base_url)

    print("\nNext steps:")
    print("  1. Set env vars:")
    print("       $env:GOOGLE_API_KEY = 'your-gemini-key'   # for xbench judge")
    print("       $env:ZEN_CLAW_API_KEY = 'your-api-key'    # or any string if no auth")
    print("  2. Start zen-claw:")
    print("       .\\scripts\\xbench_serve.ps1  (from zen-claw repo)")
    print("  3. Run evaluation:")
    print("       python xbench_evals.py --model zen-claw --dataset data/DeepSearch-2510.csv --n-repeats 1")


if __name__ == "__main__":
    main()

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


def _dict_block_contains(src: str, dict_name: str, key: str) -> bool:
    match = re.search(rf"{re.escape(dict_name)}\s*=\s*\{{(?P<body>.*?)\n\}}", src, flags=re.DOTALL)
    if not match:
        return False
    return re.search(rf'["\']{re.escape(key)}["\']\s*:', match.group("body")) is not None


def _dedupe_dict_key(src: str, dict_name: str, key: str) -> tuple[str, int]:
    match = re.search(rf"{re.escape(dict_name)}\s*=\s*\{{(?P<body>.*?)\n\}}", src, flags=re.DOTALL)
    if not match:
        return src, 0

    entry_pattern = re.compile(rf"(?m)^\s*[\"']{re.escape(key)}[\"']\s*:[^\n]*,\s*$")
    entries = list(entry_pattern.finditer(match.group("body")))
    if len(entries) <= 1:
        return src, 0

    body = match.group("body")
    keep = entries[0]
    pieces: list[str] = []
    last = 0
    for entry in entries:
        if entry is keep:
            continue
        pieces.append(body[last : entry.start()])
        last = entry.end()
    pieces.append(body[last:])
    new_body = "".join(pieces)
    new_src = src[: match.start("body")] + new_body + src[match.end("body") :]
    return new_src, len(entries) - 1


def _patch(lm_path: Path, base_url: str, judge_model: str | None = None) -> bool:
    """Apply zen-claw entries to language_models.py.

    Returns True if any change was made, False if already patched.
    """
    src = lm_path.read_text(encoding="utf-8")
    changed = False

    src, removed_prices = _dedupe_dict_key(src, "API_PRICE", "zen-claw")
    if removed_prices:
        changed = True
        print(f"[+] Removed {removed_prices} duplicate 'zen-claw' API_PRICE entries")

    # ── 1. API_ENDPOINTS ────────────────────────────────────────────────────
    endpoint_marker = '"zen-claw"'
    if endpoint_marker not in src:
        # Insert before the closing brace of API_ENDPOINTS dict.
        # The last existing entry may or may not have a trailing comma.
        pattern = r'("[\w-]+"\s*:\s*"[^"]+")(\s*\n\})'
        replacement = rf'\1,\n    "zen-claw": "{base_url}",\2'
        new_src = re.sub(pattern, replacement, src, count=1, flags=re.DOTALL)
        if new_src != src:
            src = new_src
            changed = True
            print(f"[+] Added 'zen-claw' to API_ENDPOINTS → {base_url}")
        else:
            print("[!] Could not locate API_ENDPOINTS dict — add manually:")
            print(f'    "zen-claw": "{base_url}",')
    else:
        endpoint_pattern = r'(["\']zen-claw["\']\s*:\s*)["\'][^"\']*["\']'
        new_src = re.sub(endpoint_pattern, rf'\1"{base_url}"', src, count=1)
        if new_src != src:
            src = new_src
            changed = True
            print(f"[+] Updated 'zen-claw' API_ENDPOINTS → {base_url}")
        else:
            print("[=] 'zen-claw' already in API_ENDPOINTS")

    # ── 2. API_PRICE ─────────────────────────────────────────────────────────
    if "API_PRICE" in src and not _dict_block_contains(src, "API_PRICE", "zen-claw"):
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
    zen_case = (
        '        case "zen-claw":\n'
        "            response = get_api_response(\n"
        "                prompt,\n"
        '                model="zen-claw",\n'
        '                api_key=os.getenv("ZEN_CLAW_API_KEY", "zen-claw"),\n'
        f'                base_url="{base_url}",\n'
        '                user="xbench",\n'
        "            )\n"
    )
    case_marker = 'case "zen-claw"'
    if case_marker not in src:
        # Insert before the wildcard case _ or the last case
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
        case_pattern = r'        case "zen-claw":\n.*?(?=\n        case )'
        case_match = re.search(case_pattern, src, flags=re.DOTALL)
        if case_match and (
            'user="xbench"' not in case_match.group(0)
            or "get_api_response" not in case_match.group(0)
            or f'base_url="{base_url}"' not in case_match.group(0)
        ):
            src = src[: case_match.start()] + zen_case + src[case_match.end() :]
            changed = True
            print("[+] Updated 'zen-claw' case")
        else:
            print("[=] 'zen-claw' case already in get_llm_response()")

    # ── 4. get_api_response optional user marker ───────────────────────────
    signature = (
        "def get_api_response(prompt, model, api_key, base_url=None, "
        "judge=False, think_mode=False):"
    )
    signature_with_user = (
        "def get_api_response(prompt, model, api_key, base_url=None, "
        "judge=False, think_mode=False, user=None):"
    )
    if signature in src:
        src = src.replace(signature, signature_with_user, 1)
        changed = True
        print("[+] Added optional user parameter to get_api_response()")

    if "user=user if user else None" in src:
        src = src.replace(
            "response = client.chat.completions.create(\n"
            "                model=model,\n"
            "                messages=[{\"role\": \"user\", \"content\": prompt}],\n"
            "                user=user if user else None,\n"
            "            )",
            "request_kwargs = {\n"
            "                \"model\": model,\n"
            "                \"messages\": [{\"role\": \"user\", \"content\": prompt}],\n"
            "            }\n"
            "            if user:\n"
            "                request_kwargs[\"user\"] = user\n"
            "            response = client.chat.completions.create(**request_kwargs)",
            1,
        )
        changed = True
        print("[+] Changed optional user forwarding to omit null user values")
    elif "request_kwargs[\"user\"] = user" not in src:
        pattern = (
            r"(response = client\.chat\.completions\.create\(\n"
            r"\s+model=model,\n"
            r"\s+messages=\[\{\"role\": \"user\", \"content\": prompt\}\]\n"
            r"\s+\))"
        )
        replacement = (
            "request_kwargs = {\n"
            "                \"model\": model,\n"
            "                \"messages\": [{\"role\": \"user\", \"content\": prompt}],\n"
            "            }\n"
            "            if user:\n"
            "                request_kwargs[\"user\"] = user\n"
            "            response = client.chat.completions.create(**request_kwargs)"
        )
        new_src = re.sub(pattern, replacement, src, count=1)
        if new_src != src:
            src = new_src
            changed = True
            print("[+] Forwarded optional user parameter in default OpenAI call")
        else:
            print("[!] Could not patch get_api_response() default OpenAI call for user marker")

    # ── 5. Judge model ─────────────────────────────────────────────────────
    if judge_model:
        pattern = (
            r'model="[^"]+", api_key=_or_key, '
            r'base_url="https://openrouter\.ai/api/v1", judge=judge'
        )
        replacement = (
            f'model="{judge_model}", api_key=_or_key, '
            'base_url="https://openrouter.ai/api/v1", judge=judge'
        )
        new_src = re.sub(pattern, replacement, src, count=1)
        if new_src != src:
            src = new_src
            changed = True
            print(f"[+] Patched OpenRouter judge model → {judge_model}")
        else:
            print("[=] OpenRouter judge model mapping already patched or not found")

    if changed:
        lm_path.write_text(src, encoding="utf-8")
        print(f"\nOK: Patched {lm_path}")
    else:
        print("\nOK: No changes needed — already patched")

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
    parser.add_argument(
        "--judge-model",
        default="qwen/qwen3-32b",
        help=(
            "OpenRouter judge model used by xbench's gemini-2.0 branch "
            "(default: qwen/qwen3-32b; pass '' to skip)"
        ),
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
    _patch(lm_path, args.base_url, judge_model=args.judge_model.strip() or None)

    print("\nNext steps:")
    print("  1. Set env vars:")
    print("       $env:OPENROUTER_API_KEY = 'your-openrouter-key'   # for xbench judge")
    print("       $env:ZEN_CLAW_API_KEY = 'your-api-key'    # or any string if no auth")
    print("  2. Start zen-claw:")
    print("       .\\scripts\\xbench_serve.ps1  (from zen-claw repo)")
    print("  3. Run evaluation:")
    print("       python xbench_evals.py --model zen-claw --dataset data/DeepSearch-2510.csv --n-repeats 1")


if __name__ == "__main__":
    main()

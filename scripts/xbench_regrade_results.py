"""Regrade an existing xbench CSV with the current zen-claw xbench normalizer.

The script updates score/extracted fields in a copy of the input CSV. It avoids
printing prompts, responses, or API keys.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import time
from pathlib import Path
from typing import Any

from openai import OpenAI

from zen_claw.api.openai_compat import _normalize_xbench_reply
from zen_claw.config.loader import load_config

JUDGE_PROMPT = """你是评分器。根据[正确答案]判断[回答]是否等价。

只输出三行：
最终答案: <从回答中抽取的最终答案或无>
解释: <不超过40字>
结论: <正确或错误>

[正确答案]: {correct_answer}
[回答]: {response}
""".strip()


def _load_openrouter_key() -> str:
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    try:
        cfg = load_config()
        provider = cfg.providers.get("openrouter")
        if provider and provider.api_key:
            return provider.api_key
    except Exception:
        return ""
    return ""


def _parse_judge_response(text: str) -> tuple[int, str, str]:
    extracted_match = re.search(r"最终答案[:：]\s*(.*)", text)
    conclusion_match = re.search(r"结论[:：]\s*(正确|错误)", text)
    reason_match = re.search(r"解释[:：]\s*(.*)", text)
    extracted = extracted_match.group(1).strip() if extracted_match else ""
    conclusion = conclusion_match.group(1).strip() if conclusion_match else ""
    reason = reason_match.group(1).strip() if reason_match else ""
    return (1 if conclusion == "正确" else 0), extracted, reason


def _direct_score(correct_answer: str, normalized_response: str) -> tuple[int, str, str] | None:
    match = re.search(r"最终答案[:：]\s*(.*)", normalized_response)
    extracted = match.group(1).strip() if match else ""
    if not extracted or extracted == "无":
        return 0, extracted, "normalized final answer is empty"
    if extracted == correct_answer:
        return 1, extracted, "exact normalized final answer match"
    return None


def _judge_row(
    client: OpenAI,
    *,
    judge_model: str,
    correct_answer: str,
    normalized_response: str,
    sleep_seconds: float,
) -> tuple[int, str, str]:
    direct = _direct_score(correct_answer, normalized_response)
    if direct is not None:
        return direct

    prompt = JUDGE_PROMPT.format(
        correct_answer=correct_answer,
        response=normalized_response,
    )
    try:
        response = client.chat.completions.create(
            model=judge_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=256,
            temperature=0,
        )
        text = response.choices[0].message.content or ""
        return _parse_judge_response(text)
    except Exception as exc:
        return 0, "", f"Judge API error: {type(exc).__name__}"
    finally:
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)


def _set_repeat_fields(row: dict[str, Any], score: int, extracted: str, reason: str) -> None:
    row["extracted-answer-1"] = extracted
    row["score-1"] = str(score)
    row["score-reason-1"] = reason
    row["avg_score"] = str(float(score))
    row["best_of_n"] = str(float(score))
    row["majority_vote_answer"] = extracted
    row["majority_vote_score"] = str(score)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_csv", type=Path)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--judge-model", default="qwen/qwen3-32b")
    parser.add_argument("--sleep-seconds", type=float, default=0.2)
    args = parser.parse_args()

    input_csv: Path = args.input_csv
    if not input_csv.exists():
        raise SystemExit(f"input CSV not found: {input_csv}")

    output_csv = args.output or input_csv.with_name(f"{input_csv.stem}_regraded.csv")
    key = _load_openrouter_key()
    if not key:
        raise SystemExit("OPENROUTER_API_KEY is not set and no openrouter key was found in config")

    client = OpenAI(api_key=key, base_url="https://openrouter.ai/api/v1")
    with input_csv.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
        if not rows:
            raise SystemExit("input CSV has no rows")
        fieldnames = list(rows[0].keys())

    total = len(rows)
    correct = 0
    with output_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for index, row in enumerate(rows, start=1):
            normalized_response = _normalize_xbench_reply(row.get("response-1", ""))
            score, extracted, reason = _judge_row(
                client,
                judge_model=args.judge_model,
                correct_answer=row.get("answer", ""),
                normalized_response=normalized_response,
                sleep_seconds=args.sleep_seconds,
            )
            row["response-1"] = normalized_response
            _set_repeat_fields(row, score, extracted, reason)
            correct += score
            writer.writerow(row)
            print(f"[{index}/{total}] id={row.get('id', '')} score={score} extracted={extracted[:50]!r}")

    print(f"RESULT {correct}/{total}")
    print(f"OUTPUT {output_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

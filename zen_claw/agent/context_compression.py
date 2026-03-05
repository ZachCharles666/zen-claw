"""LLM-assisted conversation context compression."""

from dataclasses import dataclass
from typing import Any


@dataclass
class CompressionPlan:
    """Compression decision and data slices."""

    should_compress: bool
    prefix_messages: list[dict[str, Any]]
    recent_messages: list[dict[str, Any]]
    summarized_upto: int


class ContextCompressor:
    """Build and apply rolling summaries for long conversations."""

    def __init__(
        self,
        trigger_messages: int = 30,
        keep_recent: int = 14,
        min_new_messages: int = 8,
        max_summary_chars: int = 1200,
        trigger_ratio: float = 0.8,
        hysteresis_ratio: float = 0.5,
        cooldown_turns: int = 5,
    ):
        self.trigger_messages = trigger_messages
        self.keep_recent = keep_recent
        self.min_new_messages = min_new_messages
        self.max_summary_chars = max_summary_chars
        self.trigger_ratio = min(0.99, max(0.1, float(trigger_ratio)))
        self.hysteresis_ratio = min(self.trigger_ratio, max(0.05, float(hysteresis_ratio)))
        self.cooldown_turns = max(0, int(cooldown_turns))

    def plan(self, messages: list[dict[str, Any]], summarized_upto: int) -> CompressionPlan:
        total = len(messages)
        if total <= self.trigger_messages:
            return CompressionPlan(False, [], messages, summarized_upto)

        cutoff = max(0, total - self.keep_recent)
        if cutoff <= summarized_upto:
            return CompressionPlan(False, [], messages, summarized_upto)

        prefix = messages[summarized_upto:cutoff]
        if len(prefix) < self.min_new_messages:
            return CompressionPlan(False, [], messages, summarized_upto)

        recent = messages[cutoff:]
        return CompressionPlan(
            should_compress=True,
            prefix_messages=prefix,
            recent_messages=recent,
            summarized_upto=cutoff,
        )

    def build_prompt(
        self,
        prefix_messages: list[dict[str, Any]],
        previous_summary: str,
    ) -> str:
        lines = []
        for msg in prefix_messages:
            role = msg.get("role", "unknown")
            content = str(msg.get("content", ""))
            lines.append(f"{role}: {content[:600]}")
        conversation = "\n".join(lines)

        return (
            "Summarize the conversation context for future turns.\n"
            "Output concise bullet points covering:\n"
            "- user goals and constraints\n"
            "- confirmed decisions\n"
            "- unresolved tasks\n"
            "- important facts to preserve\n"
            f"Keep output under {self.max_summary_chars} characters.\n\n"
            "Previous rolling summary:\n"
            f"{previous_summary or '(none)'}\n\n"
            "New conversation segment:\n"
            f"{conversation}"
        )

    def build_summary_message(self, summary: str) -> dict[str, str]:
        text = summary[: self.max_summary_chars].strip()
        return {
            "role": "assistant",
            "content": f"[Rolling Summary]\n{text}",
        }

    @staticmethod
    def estimate_tokens_from_messages(messages: list[dict[str, Any]]) -> int:
        # Rough fallback token estimator (works when provider has no tokenizer API).
        chars = 0
        for m in messages:
            c = m.get("content")
            if isinstance(c, str):
                chars += len(c)
            elif isinstance(c, list):
                for block in c:
                    if isinstance(block, dict):
                        chars += len(str(block.get("text") or "")) + len(
                            str(block.get("type") or "")
                        )
            chars += len(str(m.get("role") or "")) + 4
        return max(1, chars // 4)

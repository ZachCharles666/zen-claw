from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zen_claw.agent.loop import AgentLoop
from zen_claw.bus.queue import MessageBus
from zen_claw.providers.base import LLMProvider, LLMResponse


@dataclass
class _Session:
    key: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class _Provider(LLMProvider):
    def __init__(self, *, token_count: int | None):
        super().__init__(api_key=None, api_base=None)
        self._token_count = token_count

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        return LLMResponse(content="ok", tool_calls=[], usage={})

    def get_default_model(self) -> str:
        return "test-model"

    def count_tokens(self, messages):
        if self._token_count is None:
            raise RuntimeError("tokenizer unavailable")
        return self._token_count


def _make_loop(tmp_path: Path, token_count: int | None) -> AgentLoop:
    provider = _Provider(token_count=token_count)
    return AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        enable_planning=False,
        max_iterations=1,
        max_context_tokens=1000,
        compression_trigger_ratio=0.8,
        compression_hysteresis_ratio=0.5,
        compression_cooldown_turns=2,
    )


def test_token_budget_trigger_uses_provider_counter(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path, token_count=900)
    s = _Session(key="k")
    messages = [{"role": "user", "content": "hello"}]
    assert loop._should_compress_by_token_budget(s, messages) is True  # noqa: SLF001


def test_token_budget_fallback_estimator_when_counter_unavailable(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path, token_count=None)
    s = _Session(key="k")
    # Large text to exceed 80% with fallback estimator.
    large = "x" * 5000
    messages = [{"role": "user", "content": large}]
    assert loop._should_compress_by_token_budget(s, messages) is True  # noqa: SLF001


def test_token_budget_respects_cooldown_turns(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path, token_count=900)
    s = _Session(key="k", metadata={"compression_last_trigger_turn": 10})
    messages = [{"role": "user", "content": "a"} for _ in range(11)]
    assert loop._should_compress_by_token_budget(s, messages) is False  # noqa: SLF001

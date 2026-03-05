"""Tests for LiteLLMProvider retry / exponential-backoff logic (MEDIUM-001)."""

from unittest.mock import AsyncMock, MagicMock

import litellm
from tenacity import wait_none

from zen_claw.providers.litellm_provider import LiteLLMProvider

# ── helpers ───────────────────────────────────────────────────────────────────


def _fake_response(content: str = "hello") -> MagicMock:
    """Build a minimal litellm-style response object."""
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = None
    choice = MagicMock()
    choice.message = msg
    choice.finish_reason = "stop"
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = None
    return resp


def _provider(max_retries: int = 3) -> LiteLLMProvider:
    """Return a provider configured for fast tests (no real sleep)."""
    return LiteLLMProvider(
        api_key="test-key",
        max_retries=max_retries,
        _retry_wait=wait_none(),
    )


def _rate_limit_err() -> litellm.RateLimitError:
    return litellm.RateLimitError("rate limited", llm_provider="openai", model="gpt-4")


def _unavailable_err() -> litellm.ServiceUnavailableError:
    return litellm.ServiceUnavailableError(
        "unavailable", llm_provider="anthropic", model="claude-3"
    )


def _auth_err() -> litellm.AuthenticationError:
    return litellm.AuthenticationError("bad key", llm_provider="openai", model="gpt-4")


# ── tests ─────────────────────────────────────────────────────────────────────


async def test_chat_succeeds_on_first_try(monkeypatch):
    """Normal call — acompletion called exactly once, no retry overhead."""
    mock = AsyncMock(return_value=_fake_response("hello"))
    monkeypatch.setattr("zen_claw.providers.litellm_provider.acompletion", mock)

    result = await _provider().chat([{"role": "user", "content": "hi"}])

    assert result.finish_reason == "stop"
    assert mock.call_count == 1


async def test_chat_retries_on_rate_limit_then_succeeds(monkeypatch):
    """429 on first try → retried → succeeds on second attempt."""
    mock = AsyncMock(side_effect=[_rate_limit_err(), _fake_response("ok")])
    monkeypatch.setattr("zen_claw.providers.litellm_provider.acompletion", mock)

    result = await _provider().chat([{"role": "user", "content": "hi"}])

    assert result.finish_reason == "stop"
    assert mock.call_count == 2


async def test_chat_retries_on_service_unavailable_then_succeeds(monkeypatch):
    """503 on first try → retried → succeeds on second attempt."""
    mock = AsyncMock(side_effect=[_unavailable_err(), _fake_response("ok")])
    monkeypatch.setattr("zen_claw.providers.litellm_provider.acompletion", mock)

    result = await _provider().chat([{"role": "user", "content": "hi"}])

    assert result.finish_reason == "stop"
    assert mock.call_count == 2


async def test_chat_exhausts_retries_returns_error_response(monkeypatch):
    """All retries fail with RateLimitError → error LLMResponse, no exception raised."""
    mock = AsyncMock(side_effect=_rate_limit_err())
    monkeypatch.setattr("zen_claw.providers.litellm_provider.acompletion", mock)

    result = await _provider(max_retries=3).chat([{"role": "user", "content": "hi"}])

    assert result.finish_reason == "error"
    assert "rate" in result.content.lower() or "Error" in result.content
    assert mock.call_count == 3  # exactly max_retries attempts


async def test_chat_does_not_retry_auth_error(monkeypatch):
    """AuthenticationError is non-retryable — acompletion called exactly once."""
    mock = AsyncMock(side_effect=_auth_err())
    monkeypatch.setattr("zen_claw.providers.litellm_provider.acompletion", mock)

    result = await _provider().chat([{"role": "user", "content": "hi"}])

    assert result.finish_reason == "error"
    assert mock.call_count == 1  # no retry


async def test_chat_max_retries_one_means_single_attempt(monkeypatch):
    """max_retries=1 → only one attempt even on rate-limit error."""
    mock = AsyncMock(side_effect=_rate_limit_err())
    monkeypatch.setattr("zen_claw.providers.litellm_provider.acompletion", mock)

    result = LiteLLMProvider(api_key="test-key", max_retries=1, _retry_wait=wait_none())
    out = await result.chat([{"role": "user", "content": "hi"}])

    assert out.finish_reason == "error"
    assert mock.call_count == 1


async def test_chat_two_rate_limits_then_success(monkeypatch):
    """Two consecutive 429s then success → acompletion called 3 times."""
    mock = AsyncMock(side_effect=[_rate_limit_err(), _rate_limit_err(), _fake_response("done")])
    monkeypatch.setattr("zen_claw.providers.litellm_provider.acompletion", mock)

    result = await _provider(max_retries=5).chat([{"role": "user", "content": "hi"}])

    assert result.finish_reason == "stop"
    assert mock.call_count == 3

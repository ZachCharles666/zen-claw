"""LiteLLM provider implementation for multi-provider support."""

import os
from typing import Any

import litellm
from litellm import acompletion
from loguru import logger
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from zen_claw.providers.base import LLMProvider, LLMResponse, ToolCallRequest

# Transient errors that warrant automatic retry with exponential backoff.
_RETRYABLE_LITELLM_ERRORS = (
    litellm.RateLimitError,
    litellm.ServiceUnavailableError,
    litellm.Timeout,
)


def _log_retry_attempt(retry_state: Any) -> None:
    """Log a warning before each sleep between retries."""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    sleep_sec = retry_state.next_action.sleep if retry_state.next_action else 0
    logger.warning(
        "LLM transient error (attempt {}), retrying in {:.1f}s: {}",
        retry_state.attempt_number,
        sleep_sec,
        exc,
    )


class LiteLLMProvider(LLMProvider):
    """
    LLM provider using LiteLLM for multi-provider support.
    
    Supports OpenRouter, Anthropic, OpenAI, Gemini, and many other providers through
    a unified interface.
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        default_model: str = "anthropic/claude-opus-4-5",
        extra_headers: dict[str, str] | None = None,
        rate_limit_delay_sec: float = 0.0,
        max_retries: int = 5,
        _retry_wait: Any = None,
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.extra_headers = extra_headers or {}
        self.rate_limit_delay_sec = rate_limit_delay_sec
        self.max_retries = max(1, int(max_retries))
        # _retry_wait can be overridden in tests (e.g. wait_none()) to avoid
        # real sleeping.  In production, None means exponential jitter.
        self._retry_wait = _retry_wait

        # Detect OpenRouter by api_key prefix or explicit api_base
        self.is_openrouter = (
            (api_key and api_key.startswith("sk-or-")) or
            (api_base and "openrouter" in api_base)
        )

        # Detect AiHubMix by api_base
        self.is_aihubmix = bool(api_base and "aihubmix" in api_base)

        # Track if using custom endpoint (vLLM, etc.)
        self.is_vllm = bool(api_base) and not self.is_openrouter and not self.is_aihubmix

        # Configure LiteLLM based on provider
        if api_key:
            if self.is_openrouter:
                # OpenRouter mode - set key
                os.environ["OPENROUTER_API_KEY"] = api_key
            elif self.is_aihubmix:
                # AiHubMix gateway - OpenAI-compatible
                os.environ["OPENAI_API_KEY"] = api_key
            elif self.is_vllm:
                # vLLM/custom endpoint - uses OpenAI-compatible API
                os.environ["HOSTED_VLLM_API_KEY"] = api_key
            elif "deepseek" in default_model:
                os.environ.setdefault("DEEPSEEK_API_KEY", api_key)
            elif "anthropic" in default_model:
                os.environ.setdefault("ANTHROPIC_API_KEY", api_key)
            elif "openai" in default_model or "gpt" in default_model:
                os.environ.setdefault("OPENAI_API_KEY", api_key)
            elif "gemini" in default_model.lower():
                os.environ.setdefault("GEMINI_API_KEY", api_key)
            elif "zhipu" in default_model or "glm" in default_model or "zai" in default_model:
                os.environ.setdefault("ZAI_API_KEY", api_key)
                os.environ.setdefault("ZHIPUAI_API_KEY", api_key)
            elif "dashscope" in default_model or "qwen" in default_model.lower():
                os.environ.setdefault("DASHSCOPE_API_KEY", api_key)
            elif "groq" in default_model:
                os.environ.setdefault("GROQ_API_KEY", api_key)
            elif "moonshot" in default_model or "kimi" in default_model:
                os.environ.setdefault("MOONSHOT_API_KEY", api_key)
                os.environ.setdefault("MOONSHOT_API_BASE", api_base or "https://api.moonshot.cn/v1")

        if api_base:
            litellm.api_base = api_base

        # Disable LiteLLM logging noise
        litellm.suppress_debug_info = True

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        """
        Send a chat completion request via LiteLLM.
        
        Args:
            messages: List of message dicts with 'role' and 'content'.
            tools: Optional list of tool definitions in OpenAI format.
            model: Model identifier (e.g., 'anthropic/claude-sonnet-4-5').
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.
        
        Returns:
            LLMResponse with content and/or tool calls.
        """
        model = model or self.default_model

        # Auto-prefix model names for known providers
        # (keywords, target_prefix, skip_if_starts_with)
        _prefix_rules = [
            (("glm", "zhipu"), "zai", ("zhipu/", "zai/", "openrouter/", "hosted_vllm/")),
            (("qwen", "dashscope"), "dashscope", ("dashscope/", "openrouter/")),
            (("moonshot", "kimi"), "moonshot", ("moonshot/", "openrouter/")),
            (("gemini",), "gemini", ("gemini/",)),
        ]
        model_lower = model.lower()
        for keywords, prefix, skip in _prefix_rules:
            if any(kw in model_lower for kw in keywords) and not any(model.startswith(s) for s in skip):
                model = f"{prefix}/{model}"
                break

        # Gateway/endpoint-specific prefixes (detected by api_base/api_key, not model name)
        if self.is_openrouter and not model.startswith("openrouter/"):
            model = f"openrouter/{model}"
        elif self.is_aihubmix:
            model = f"openai/{model.split('/')[-1]}"
        elif self.is_vllm:
            model = f"hosted_vllm/{model}"

        # kimi-k2.5 only supports temperature=1.0
        if "kimi-k2.5" in model.lower():
            temperature = 1.0

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        # Pass api_base directly for custom endpoints (vLLM, etc.)
        if self.api_base:
            kwargs["api_base"] = self.api_base

        # Pass extra headers (e.g. APP-Code for AiHubMix)
        if self.extra_headers:
            kwargs["extra_headers"] = self.extra_headers

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        if self.rate_limit_delay_sec > 0:
            import asyncio
            await asyncio.sleep(self.rate_limit_delay_sec)

        _wait = (
            self._retry_wait
            if self._retry_wait is not None
            else wait_exponential_jitter(initial=1, max=60, jitter=2)
        )
        try:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception_type(_RETRYABLE_LITELLM_ERRORS),
                wait=_wait,
                stop=stop_after_attempt(self.max_retries),
                before_sleep=_log_retry_attempt,
                reraise=True,
            ):
                with attempt:
                    response = await acompletion(**kwargs)
            return self._parse_response(response)
        except _RETRYABLE_LITELLM_ERRORS as e:
            logger.error(
                "LLM call failed after {} attempts: {}",
                self.max_retries,
                e,
            )
            return LLMResponse(
                content=f"Error calling LLM (rate limited after {self.max_retries} retries): {str(e)}",
                finish_reason="error",
            )
        except Exception as e:
            # Non-retryable error (auth, bad request, …) — fail fast.
            return LLMResponse(
                content=f"Error calling LLM: {str(e)}",
                finish_reason="error",
            )

    def _parse_response(self, response: Any) -> LLMResponse:
        """Parse LiteLLM response into our standard format."""
        choice = response.choices[0]
        message = choice.message

        tool_calls = []
        if hasattr(message, "tool_calls") and message.tool_calls:
            for tc in message.tool_calls:
                # Parse arguments from JSON string if needed
                args = tc.function.arguments
                if isinstance(args, str):
                    import json
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"raw": args}

                tool_calls.append(ToolCallRequest(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=args,
                ))

        usage = {}
        if hasattr(response, "usage") and response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
        )

    def get_default_model(self) -> str:
        """Get the default model."""
        return self.default_model



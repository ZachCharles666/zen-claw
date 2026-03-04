import time

import pytest

from zen_claw.bus.queue import MessageBus
from zen_claw.channels.manager import ChannelManager, _TokenBucketRateLimiter
from zen_claw.config.schema import ChannelRateLimitConfig, Config


@pytest.mark.asyncio
async def test_token_bucket_rate_limiter_waits_when_burst_exhausted() -> None:
    limiter = _TokenBucketRateLimiter(rate_per_sec=5.0, burst=1)
    start = time.monotonic()
    await limiter.acquire("discord:u1")
    await limiter.acquire("discord:u1")
    elapsed = time.monotonic() - start
    assert elapsed >= 0.15


@pytest.mark.asyncio
async def test_token_bucket_rate_limiter_isolated_by_key() -> None:
    limiter = _TokenBucketRateLimiter(rate_per_sec=5.0, burst=1)
    start = time.monotonic()
    await limiter.acquire("discord:u1")
    await limiter.acquire("discord:u2")
    elapsed = time.monotonic() - start
    assert elapsed < 0.15


@pytest.mark.asyncio
async def test_token_bucket_try_acquire_reports_retry_after() -> None:
    limiter = _TokenBucketRateLimiter(rate_per_sec=5.0, burst=1)
    ok1, retry1 = await limiter.try_acquire("discord:u1")
    ok2, retry2 = await limiter.try_acquire("discord:u1")
    assert ok1 is True
    assert retry1 == 0.0
    assert ok2 is False
    assert retry2 > 0.0


def test_channel_manager_uses_per_channel_rate_limit_override(tmp_path) -> None:
    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path / "ws")
    cfg.channels.outbound_rate_limit_per_sec = 9.0
    cfg.channels.outbound_rate_limit_burst = 9
    cfg.channels.outbound_rate_limit_mode = "delay"
    cfg.channels.outbound_rate_limit_by_channel = {
        "discord": ChannelRateLimitConfig(per_sec=1.0, burst=2, mode="drop"),
    }
    mgr = ChannelManager(cfg, MessageBus())
    limiter, mode = mgr._resolve_rate_limit("discord")
    assert mode == "drop"
    assert limiter.rate_per_sec == 1.0
    assert limiter.burst == 2

    limiter2, mode2 = mgr._resolve_rate_limit("telegram")
    assert mode2 == "delay"
    assert limiter2.rate_per_sec == 9.0
    assert limiter2.burst == 9

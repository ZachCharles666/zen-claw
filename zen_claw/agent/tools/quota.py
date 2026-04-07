"""Global quota management for tool execution."""

from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock

from loguru import logger


class _LocalSlidingWindow:
    """In-memory sliding window counter for quota enforcement without Redis."""

    def __init__(self):
        self._lock = Lock()
        self._windows: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str, limit: int, window_sec: int) -> bool:
        now = time.monotonic()
        cutoff = now - window_sec
        with self._lock:
            history = self._windows[key]
            # Prune expired entries.
            history[:] = [t for t in history if t > cutoff]
            if len(history) >= limit:
                return False
            history.append(now)
            return True


class QuotaEngine:
    """
    Global quota engine using Redis with local fallback.

    Enforces tenant-level rate limits and usage budgets.
    Fail-Closed: if Redis is unavailable in cluster mode, falls back to
    in-memory sliding window so that basic rate limiting still applies.
    """

    def __init__(self, redis_url: str | None = None, enabled: bool = True):
        self.enabled = enabled
        self.redis_url = redis_url
        self._redis = None
        self._local = _LocalSlidingWindow()
        if enabled and redis_url:
            try:
                import redis

                self._redis = redis.from_url(redis_url, decode_responses=True)
            except ImportError:
                logger.warning("redis-py not installed, QuotaEngine will use local fallback.")
            except Exception as e:
                logger.error(f"Failed to connect to Redis for quotas: {e}")

    async def check_quota(
        self,
        tenant_id: str,
        tool_name: str,
        *,
        capability: str = "",
        capability_limit: int | None = None,
    ) -> bool:
        """
        Check if the tenant has remaining quota for the tool/capability.

        When *capability_limit* is provided, a second per-capability check is
        performed in addition to the per-tool check.

        Returns:
            True if allowed, False if denied (quota exceeded or error).
        """
        if not self.enabled:
            return True

        tool_key = f"quota:tenant:{tenant_id}:{tool_name}:rate"
        tool_limit = 100
        tool_window = 3600  # 1 hour

        # Per-tool check.
        if not self._check(tool_key, tool_limit, tool_window):
            logger.warning(f"Quota exceeded for tool '{tool_name}' tenant '{tenant_id}'")
            return False

        # Per-capability check (if configured).
        if capability and capability_limit is not None and capability_limit > 0:
            cap_key = f"quota:tenant:{tenant_id}:capability:{capability}:rate"
            if not self._check(cap_key, capability_limit, tool_window):
                logger.warning(
                    f"Capability quota exceeded for '{capability}' tenant '{tenant_id}'"
                )
                return False

        return True

    def _check(self, key: str, limit: int, window_sec: int) -> bool:
        """Check a single quota key, falling back to local if Redis is unavailable."""
        if self._redis is not None:
            try:
                lua = """
                local current = redis.call("INCR", KEYS[1])
                if current == 1 then
                    redis.call("EXPIRE", KEYS[1], ARGV[2])
                end
                if current > tonumber(ARGV[1]) then
                    return 0
                end
                return 1
                """
                allowed = self._redis.eval(lua, 1, key, limit, window_sec)
                return bool(allowed)
            except Exception as e:
                logger.error(f"Redis quota check error, falling back to local: {e}")

        # Local in-memory fallback.
        return self._local.check(key, limit, window_sec)

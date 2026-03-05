"""Global quota management for tool execution."""

from loguru import logger


class QuotaEngine:
    """
    Global quota engine using Redis.

    Enforces tenant-level rate limits and usage budgets.
    Fail-Closed: if Redis is unavailable in cluster mode, access is denied.
    """

    def __init__(self, redis_url: str | None = None, enabled: bool = True):
        self.enabled = enabled
        self.redis_url = redis_url
        self._redis = None
        if enabled and redis_url:
            try:
                import redis

                self._redis = redis.from_url(redis_url, decode_responses=True)
            except ImportError:
                logger.warning("redis-py not installed, QuotaEngine will fail-closed if enabled.")
            except Exception as e:
                logger.error(f"Failed to connect to Redis for quotas: {e}")

    async def check_quota(self, tenant_id: str, tool_name: str) -> bool:
        """
        Check if the tenant has remaining quota for the tool.

        Returns:
            True if allowed, False if denied (quota exceeded or Redis error).
        """
        if not self.enabled:
            return True

        if self._redis is None:
            # Fail-Closed if enabled but redis is missing
            logger.error("Quota check failed: Redis client not initialized (Fail-Closed)")
            return False

        try:
            # Use Lua script for atomic increment and expiry
            # KEY 1: quota:tenant:{tenant_id}:{tool_name}:rate
            # ARGV 1: limit, 2: window_sec
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
            # Placeholder values for now
            limit = 100
            window = 3600  # 1 hour

            key = f"quota:tenant:{tenant_id}:{tool_name}:rate"
            allowed = self._redis.eval(lua, 1, key, limit, window)
            return bool(allowed)
        except Exception as e:
            logger.error(f"Quota check error (Fail-Closed): {e}")
            return False

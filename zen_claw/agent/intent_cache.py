"""Intent-level response cache — C3.

Caches the text content of direct-intent responses (zero-LLM replies) to
avoid repeating identical computation and API calls for frequent, deterministic
queries such as exchange rates, weather, or hash lookups.

Design
------
* **Key**: ``(intent_name, normalised_query_hash)``
  where *normalised_query_hash* is a SHA-256 truncation of the lowercased,
  whitespace-normalised user message.
* **TTL**: Per intent name, configurable.  Defaults to ``300`` seconds.
  ``TIME`` queries should **not** be cached (TTL = 0 disables caching).
* **Capacity**: LRU eviction with a configurable ``maxsize`` (default 256).
* **Hit stats**: ``hits`` / ``misses`` / ``evictions`` counters for Dashboard.

Usage
-----
::

    cache = IntentCache(ttl_by_intent={"exchange_rate": 3600, "time": 0})

    result = cache.get("exchange_rate", user_message)
    if result is None:
        result = expensive_computation()
        cache.put("exchange_rate", user_message, result)
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

# ── Cache entry ───────────────────────────────────────────────────────────────

@dataclass
class CacheEntry:
    content: str
    expires_at: float   # Unix timestamp; math.inf when TTL == 0 means "never set"


# ── Default TTLs (seconds) ────────────────────────────────────────────────────

DEFAULT_TTL_BY_INTENT: dict[str, int] = {
    "time":          0,       # never cache (always real-time)
    "weather":       1800,    # 30 minutes
    "exchange_rate": 3600,    # 1 hour
    "fixed_site_fetch": 300,  # 5 minutes
    # C1 contracts
    "calculator":    0,       # pure computation — no network, negligible cost
    "unit_convert":  0,
    "text_stats":    0,
    "encode":        0,
    "hash_digest":   0,
    "json_format":   0,
    "color_convert": 0,
    "ip_lookup":     3600,    # IP geo rarely changes
    "timezone_list": 86400,   # timezone data is very stable
}
DEFAULT_TTL: int = 300        # fallback for unknown intent names


# ── Cache ─────────────────────────────────────────────────────────────────────

class IntentCache:
    """LRU cache with per-intent TTLs for direct-response content.

    Parameters
    ----------
    maxsize:
        Maximum number of entries before LRU eviction kicks in.
    ttl_by_intent:
        Per-intent TTL in seconds.  ``0`` disables caching for that intent.
        Falls back to *default_ttl* for unknown intent names.
    default_ttl:
        Fallback TTL for intent names not listed in *ttl_by_intent*.
    """

    def __init__(
        self,
        *,
        maxsize: int = 256,
        ttl_by_intent: dict[str, int] | None = None,
        default_ttl: int = DEFAULT_TTL,
    ) -> None:
        self._maxsize = max(1, int(maxsize))
        self._ttl_map: dict[str, int] = dict(DEFAULT_TTL_BY_INTENT)
        if ttl_by_intent:
            self._ttl_map.update(ttl_by_intent)
        self._default_ttl = max(0, int(default_ttl))
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    # ── public API ────────────────────────────────────────────────────────────

    def get(self, intent_name: str, query: str) -> str | None:
        """Return the cached response for *(intent_name, query)*, or ``None``.

        Returns ``None`` when:
        - the entry does not exist;
        - the entry has expired;
        - TTL for this intent is 0 (caching disabled).
        """
        if self._ttl_for(intent_name) == 0:
            self._misses += 1
            return None

        key = self._make_key(intent_name, query)
        entry = self._store.get(key)
        if entry is None:
            self._misses += 1
            return None
        if time.time() > entry.expires_at:
            del self._store[key]
            self._misses += 1
            return None
        # LRU: move to end (most recently used)
        self._store.move_to_end(key)
        self._hits += 1
        return entry.content

    def put(self, intent_name: str, query: str, content: str) -> None:
        """Store *content* under *(intent_name, query)*.

        No-op when TTL for this intent is 0.
        """
        ttl = self._ttl_for(intent_name)
        if ttl == 0:
            return

        key = self._make_key(intent_name, query)
        expires_at = time.time() + ttl

        if key in self._store:
            self._store.move_to_end(key)
            self._store[key] = CacheEntry(content=content, expires_at=expires_at)
        else:
            # Evict LRU entry if at capacity
            while len(self._store) >= self._maxsize:
                self._store.popitem(last=False)
                self._evictions += 1
            self._store[key] = CacheEntry(content=content, expires_at=expires_at)

    def invalidate(self, intent_name: str, query: str) -> bool:
        """Remove a specific entry.  Returns ``True`` if it existed."""
        key = self._make_key(intent_name, query)
        if key in self._store:
            del self._store[key]
            return True
        return False

    def clear(self) -> None:
        """Remove all entries and reset stats."""
        self._store.clear()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def stats(self) -> dict[str, Any]:
        """Return hit/miss/eviction counters and current size."""
        total = self._hits + self._misses
        return {
            "size": len(self._store),
            "maxsize": self._maxsize,
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
            "hit_rate": round(self._hits / total, 4) if total > 0 else 0.0,
        }

    # ── private ───────────────────────────────────────────────────────────────

    def _ttl_for(self, intent_name: str) -> int:
        return self._ttl_map.get(intent_name.lower(), self._default_ttl)

    @staticmethod
    def _normalise(query: str) -> str:
        """Lower-case and collapse whitespace for stable key generation."""
        return re.sub(r"\s+", " ", query.strip().lower())

    @staticmethod
    def _make_key(intent_name: str, query: str) -> str:
        normalised = IntentCache._normalise(query)
        digest = hashlib.sha256(normalised.encode()).hexdigest()[:16]
        return f"{intent_name.lower()}:{digest}"

"""Tests for C3 — Intent-level LRU + TTL response cache."""

from __future__ import annotations

import time

from zen_claw.agent.intent_cache import IntentCache

# ── basic get/put ─────────────────────────────────────────────────────────────

def test_get_miss_returns_none():
    cache = IntentCache()
    assert cache.get("exchange_rate", "USD to EUR") is None


def test_put_and_get_hit():
    cache = IntentCache()
    cache.put("exchange_rate", "USD to EUR", "1 USD = 0.92 EUR")
    result = cache.get("exchange_rate", "USD to EUR")
    assert result == "1 USD = 0.92 EUR"


def test_put_disabled_ttl_zero():
    """Intent with TTL=0 should never be cached."""
    cache = IntentCache()
    cache.put("time", "what time is it", "12:00")
    assert cache.get("time", "what time is it") is None


def test_query_normalisation():
    """Queries differing only in case/whitespace should hit the same key."""
    cache = IntentCache()
    cache.put("exchange_rate", "usd to eur", "0.92")
    assert cache.get("exchange_rate", "USD  to  EUR") == "0.92"


# ── TTL expiry ────────────────────────────────────────────────────────────────

def test_entry_expires_after_ttl(monkeypatch):
    """After TTL seconds the entry should not be returned."""
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    # Use custom TTL override so default_ttl_by_intent does not interfere
    cache = IntentCache(ttl_by_intent={"weather": 1}, default_ttl=1)
    cache.put("weather", "London weather", "Cloudy")

    # Advance clock past TTL
    monkeypatch.setattr(time, "time", lambda: now + 2)
    assert cache.get("weather", "London weather") is None


def test_entry_still_valid_within_ttl(monkeypatch):
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    cache = IntentCache(default_ttl=60)
    cache.put("exchange_rate", "EUR to JPY", "160")

    monkeypatch.setattr(time, "time", lambda: now + 30)
    assert cache.get("exchange_rate", "EUR to JPY") == "160"


# ── LRU eviction ─────────────────────────────────────────────────────────────

def test_lru_eviction_when_full():
    """Oldest (least recently used) entry is evicted when maxsize reached."""
    cache = IntentCache(maxsize=3, default_ttl=3600)
    cache.put("ip_lookup", "1.1.1.1", "Cloudflare")
    cache.put("ip_lookup", "8.8.8.8", "Google")
    cache.put("ip_lookup", "9.9.9.9", "Quad9")
    # All three fill the cache
    assert cache.stats()["size"] == 3
    # Adding a 4th should evict the LRU (1.1.1.1 — inserted first, not re-used)
    cache.put("ip_lookup", "8.8.4.4", "Google secondary")
    assert cache.stats()["size"] == 3
    assert cache.get("ip_lookup", "1.1.1.1") is None  # evicted
    assert cache.get("ip_lookup", "8.8.4.4") is not None


# ── stats ─────────────────────────────────────────────────────────────────────

def test_stats_counts_hits_and_misses():
    cache = IntentCache(default_ttl=3600)
    cache.put("timezone_list", "China", "Asia/Shanghai")
    cache.get("timezone_list", "China")   # hit
    cache.get("timezone_list", "Narnia")  # miss
    s = cache.stats()
    assert s["hits"] == 1
    assert s["misses"] == 1
    assert s["hit_rate"] == 0.5


def test_clear_resets_cache():
    cache = IntentCache(default_ttl=3600)
    cache.put("exchange_rate", "USD to GBP", "0.79")
    cache.clear()
    assert cache.stats()["size"] == 0
    assert cache.get("exchange_rate", "USD to GBP") is None

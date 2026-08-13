import time

from app.core.cache import RateLimiter, ResultCache


def test_result_cache_roundtrip_and_clear():
    cache = ResultCache(ttl_s=60)
    cache.set(("view", "v1", ()), {"rows": 1})
    assert cache.get(("view", "v1", ())) == {"rows": 1}
    assert cache.get(("view", "other", ())) is None
    cache.clear()
    assert cache.get(("view", "v1", ())) is None


def test_result_cache_ttl_expiry():
    cache = ResultCache(ttl_s=0.05)
    cache.set(("k",), 1)
    time.sleep(0.08)
    assert cache.get(("k",)) is None


def test_rate_limiter_blocks_over_limit():
    limiter = RateLimiter(max_per_minute=3)
    assert all(limiter.allow("s") for _ in range(3))
    assert limiter.allow("s") is False
    assert limiter.allow("other-session") is True

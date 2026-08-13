"""Result caching + rate limiting (in-memory, single-process by design)."""

import threading
import time
from collections import deque

from cachetools import TTLCache


class ResultCache:
    """Short-TTL cache for executed query results. Flushed on DB refresh."""

    def __init__(self, maxsize: int = 256, ttl_s: int = 600) -> None:
        self._cache: TTLCache = TTLCache(maxsize=maxsize, ttl=ttl_s)
        self._lock = threading.Lock()

    def get(self, key: tuple):
        with self._lock:
            return self._cache.get(key)

    def set(self, key: tuple, value) -> None:
        with self._lock:
            self._cache[key] = value

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)


class RateLimiter:
    """Sliding-window per-key limiter (e.g. chat requests per session)."""

    def __init__(self, max_per_minute: int = 20) -> None:
        self._max = max_per_minute
        self._events: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            window = self._events.setdefault(key, deque())
            while window and now - window[0] > 60:
                window.popleft()
            if len(window) >= self._max:
                return False
            window.append(now)
            return True

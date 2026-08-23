"""
core/cache.py

Rate limiting (and, later, disk caching) shared by every retrieval client.

IMPORTANT: NCBI E-utilities rate limit is 3 req/s without an API key,
10 req/s with one. This was verified directly against NLM's own current
support docs on 2026-08-23 -- do not "helpfully" bump this to 5/9 based on
AI-generated research; that figure belongs to the separate NCBI Datasets
API, not E-utilities, and mixing them up will get your IP blocked.
"""

from __future__ import annotations

import threading
import time


class TokenBucket:
    """
    Simple thread-safe token bucket rate limiter.

    Usage:
        bucket = TokenBucket(rate_per_second=3)
        bucket.acquire()  # blocks until a token is available
        # ... make request ...
    """

    def __init__(self, rate_per_second: float, burst: int | None = None):
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        self.rate = rate_per_second
        self.capacity = burst if burst is not None else max(1, int(rate_per_second))
        self._tokens = float(self.capacity)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._last_refill = now

    def acquire(self) -> None:
        """Blocks (sleeps) until a token is available, then consumes one."""
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                # not enough tokens yet -- compute how long until one frees up
                deficit = 1 - self._tokens
                wait_time = deficit / self.rate
            time.sleep(wait_time)


def ncbi_rate_limiter(has_api_key: bool) -> TokenBucket:
    """
    Returns the correct rate limiter for NCBI E-utilities traffic.
    Throttled slightly below the documented ceiling (3 or 10 req/s) to
    leave headroom for network jitter, per NCBI's own recommendation.
    """
    ceiling = 10 if has_api_key else 3
    safe_rate = ceiling * 0.9  # e.g. 2.7/s or 9/s
    return TokenBucket(rate_per_second=safe_rate)
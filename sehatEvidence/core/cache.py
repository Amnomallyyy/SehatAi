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

    def __init__(self, rate_per_second: float, burst: int = 1):
        """
        burst defaults to 1 -- i.e. NO bursting. This matters: NCBI counts
        requests in a strict sliding window, so a bucket that starts full
        with N tokens can fire N requests instantly and then keep refilling,
        briefly exceeding the documented ceiling and earning a 429. Only
        raise burst above 1 for APIs that explicitly tolerate it.
        """
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        self.rate = rate_per_second
        self.capacity = max(1, burst)
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

    Ceiling is 3 req/s without an API key, 10 with one. We throttle to
    roughly 2/3 of the ceiling rather than 90% of it: NCBI's counting is
    strict, network jitter can bunch requests together, and a 429 costs a
    whole query's worth of evidence. Being conservative here is cheap;
    getting rate-limited mid-demo is not.

    Note this is a PER-CLIENT limiter -- if you construct multiple
    PubMedClients they will not share a budget. Construct one and reuse it
    (which is what retrieve.py does).
    """
    if has_api_key:
        return TokenBucket(rate_per_second=7, burst=1)
    return TokenBucket(rate_per_second=2, burst=1)
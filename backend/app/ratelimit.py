"""
Small in-process rate limiter for the unauthenticated auth routes.

Login is the one place an attacker gets unlimited bcrypt-checked guesses
against a known email; signup is the one place anyone can write rows
without an account. Both get a sliding-window cap per client IP (and, for
login, per IP + email so one noisy user can't lock out a whole NAT'd
classroom). In-memory is deliberate: this API runs as a single process per
container, and the gateway (deploy/nginx.conf) adds its own per-IP limit
in front -- this is the second layer, not a distributed quota system.

Behind the gateway, uvicorn runs with --proxy-headers so request.client
is the real browser's address, not nginx's.
"""
import threading
import time
from collections import defaultdict, deque
from typing import Callable, Deque, Dict

from fastapi import HTTPException, Request, status

_lock = threading.Lock()
_hits: Dict[str, Deque[float]] = defaultdict(deque)
_last_sweep = time.monotonic()


def _check(key: str, limit: int, window_seconds: float) -> None:
    global _last_sweep
    now = time.monotonic()
    with _lock:
        if now - _last_sweep > 300:
            for k in [k for k, q in _hits.items() if not q or now - q[-1] > window_seconds]:
                del _hits[k]
            _last_sweep = now
        q = _hits[key]
        while q and now - q[0] > window_seconds:
            q.popleft()
        if len(q) >= limit:
            retry_after = int(window_seconds - (now - q[0])) + 1
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many attempts. Please wait a moment and try again.",
                headers={"Retry-After": str(retry_after)},
            )
        q.append(now)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def limit_per_ip(bucket: str, limit: int, window_seconds: float) -> Callable[[Request], None]:
    """FastAPI dependency factory: at most `limit` calls per client IP per
    `window_seconds` for this bucket."""

    def dependency(request: Request) -> None:
        _check(f"{bucket}:{client_ip(request)}", limit, window_seconds)

    return dependency


def check_login_attempt(request: Request, email: str, limit: int = 10, window_seconds: float = 300) -> None:
    _check(f"login:{client_ip(request)}:{email.lower()}", limit, window_seconds)


def reset() -> None:
    """Test helper."""
    with _lock:
        _hits.clear()

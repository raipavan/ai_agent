"""Thread-safe sliding-window rate limiting (pure stdlib + FastAPI/Starlette)."""

from __future__ import annotations

import functools
import inspect
import math
import re
import threading
import time
from collections import deque
from typing import Callable, Optional, Tuple

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

_UNITS = {
    "second": 1.0,
    "minute": 60.0,
    "hour": 3600.0,
    "day": 86400.0,
}

_RATE_RE = re.compile(r"^\s*(\d+)\s*/\s*([a-z]+)\s*$", re.IGNORECASE)


class SlidingWindowLimiter:
    """Thread-safe sliding-window rate limiter.

    Tracks a deque of ``time.monotonic()`` timestamps per key. Old entries are
    pruned on every check; a key is over limit when its in-window entry count
    reaches the configured maximum.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[str, deque] = {}
        self._rate_cache: dict[str, Tuple[int, float]] = {}
        self._last_cleanup = 0.0

    def _parse_rate(self, rate: str) -> Tuple[int, float]:
        """Parse ``"N/second"``, ``"N/minute"``, ``"N/hour"`` or ``"N/day"``
        into ``(max_calls, window_seconds)``."""
        cached = self._rate_cache.get(rate)
        if cached is not None:
            return cached

        match = _RATE_RE.match(rate)
        if not match:
            raise ValueError(
                f"Invalid rate string {rate!r}; expected e.g. '5/minute' or '100/hour'"
            )
        count = int(match.group(1))
        unit = match.group(2).lower()
        window = None
        for name, seconds in _UNITS.items():
            if unit == name or unit == name + "s":
                window = seconds
                break
        if window is None:
            raise ValueError(
                f"Invalid rate unit {unit!r}; expected second/minute/hour/day"
            )
        parsed: Tuple[int, float] = (count, window)
        self._rate_cache[rate] = parsed
        return parsed

    def _check(self, key: str, rate: str) -> Tuple[bool, float]:
        """Record one attempt for ``key`` and return ``(allowed, retry_after)``.

        ``retry_after`` is the number of seconds until the key is allowed again
        (0 when allowed)."""
        count, window = self._parse_rate(rate)
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = deque()
                self._buckets[key] = bucket

            cutoff = now - window
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            if len(bucket) < count:
                bucket.append(now)
                return True, 0.0

            retry_after = max(bucket[0] + window - now, 0.0)
            self._maybe_cleanup(now)
            return False, retry_after

    def _maybe_cleanup(self, now: float) -> None:
        """Drop empty buckets occasionally so memory stays bounded."""
        if len(self._buckets) <= 4096 or now - self._last_cleanup < 60.0:
            return
        self._last_cleanup = now
        for key in [k for k, b in self._buckets.items() if not b]:
            del self._buckets[key]

    def reset(self) -> None:
        """Clear all windows (used in tests)."""
        with self._lock:
            self._buckets.clear()
            self._rate_cache.clear()

    def _extract_request(self, args: tuple, kwargs: dict) -> Optional[Request]:
        request = kwargs.get("request")
        if isinstance(request, Request):
            return request
        for arg in args:
            if isinstance(arg, Request):
                return arg
        return None

    def _enforce(self, rate: str, func: Callable, args: tuple, kwargs: dict) -> None:
        request = self._extract_request(args, kwargs)
        if request is not None and request.client is not None:
            ip = request.client.host
        else:
            ip = "unknown"
        key = f"{func.__name__}:{ip}"
        allowed, retry_after = self._check(key, rate)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please try again later.",
                headers={"Retry-After": str(max(1, int(math.ceil(retry_after))))},
            )

    def limit(self, rate: str) -> Callable:
        """Decorator factory; usage mirrors slowapi::

            @router.post("/api/login")
            @limiter.limit("5/minute")
            async def login(request: Request, data: LoginRequest): ...
        """

        def decorator(func: Callable) -> Callable:
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                self._enforce(rate, func, args, kwargs)
                return await func(*args, **kwargs)

            @functools.wraps(func)
            def sync_wrapper(*args, **kwargs):
                self._enforce(rate, func, args, kwargs)
                return func(*args, **kwargs)

            return async_wrapper if inspect.iscoroutinefunction(func) else sync_wrapper

        return decorator


limiter = SlidingWindowLimiter()

_SKIP_PREFIXES = ("/vobiz", "/ws", "/health", "/static", "/media")


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Global per-IP rate limit; never throttles telephony/websocket/health/static paths."""

    def __init__(self, app, default_rate: str = "300/minute") -> None:
        super().__init__(app)
        self._default_rate = default_rate
        self._limiter = limiter
        self._limiter._parse_rate(default_rate)  # fail fast on bad config

    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)
        path = request.url.path
        if any(path.startswith(prefix) for prefix in _SKIP_PREFIXES):
            return await call_next(request)

        ip = request.client.host if request.client is not None else "unknown"
        allowed, retry_after = self._limiter._check(f"middleware:{ip}", self._default_rate)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests"},
                headers={
                    "Retry-After": str(max(1, int(math.ceil(retry_after)))),
                },
            )
        return await call_next(request)

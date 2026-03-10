from __future__ import annotations

import asyncio
import hashlib
import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(slots=True)
class RateLimitDecision:
    limit: int
    window_seconds: int
    remaining: int
    reset_after_seconds: int
    exceeded: bool
    retry_after_seconds: int | None = None


class SlidingWindowRateLimiter:
    def __init__(self, *, limit: int, window_seconds: int):
        if limit <= 0:
            raise ValueError("limit must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self._lock = asyncio.Lock()
        self._events: dict[str, deque[float]] = defaultdict(deque)

    async def evaluate(self, key: str, *, now: float | None = None) -> RateLimitDecision:
        timestamp = time.monotonic() if now is None else now
        async with self._lock:
            bucket = self._events[key]
            cutoff = timestamp - self.window_seconds
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            if len(bucket) >= self.limit:
                retry_after = max(1, math.ceil(self.window_seconds - (timestamp - bucket[0])))
                return RateLimitDecision(
                    limit=self.limit,
                    window_seconds=self.window_seconds,
                    remaining=0,
                    reset_after_seconds=retry_after,
                    exceeded=True,
                    retry_after_seconds=retry_after,
                )

            bucket.append(timestamp)
            remaining = max(0, self.limit - len(bucket))
            reset_after = self.window_seconds
            if bucket:
                reset_after = max(1, math.ceil(self.window_seconds - (timestamp - bucket[0])))
            return RateLimitDecision(
                limit=self.limit,
                window_seconds=self.window_seconds,
                remaining=remaining,
                reset_after_seconds=reset_after,
                exceeded=False,
            )


def is_exempt_path(path: str, exempt_paths: list[str]) -> bool:
    return any(path == exempt or path.startswith(f"{exempt}/") for exempt in exempt_paths)


def build_rate_limit_key(
    *,
    operator_id_header: str,
    headers: dict | object,
    client_host: str | None,
) -> str:
    operator_id = getattr(headers, "get", lambda *_args, **_kwargs: None)(operator_id_header)
    if operator_id:
        return f"operator:{str(operator_id).strip()}"
    authorization = getattr(headers, "get", lambda *_args, **_kwargs: None)("authorization")
    if authorization:
        token_hash = hashlib.sha256(str(authorization).encode("utf-8")).hexdigest()[:16]
        return f"auth:{token_hash}"
    return f"ip:{client_host or 'unknown'}"

from __future__ import annotations

import asyncio
import hashlib
import math
import time
from collections import OrderedDict, deque
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
    def __init__(self, *, limit: int, window_seconds: int, max_buckets: int = 4096):
        if limit <= 0:
            raise ValueError("limit must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if max_buckets <= 0:
            raise ValueError("max_buckets must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_buckets = max_buckets
        self._lock = asyncio.Lock()
        self._events: OrderedDict[str, deque[float]] = OrderedDict()

    async def evaluate(self, key: str, *, now: float | None = None) -> RateLimitDecision:
        timestamp = time.monotonic() if now is None else now
        async with self._lock:
            cutoff = timestamp - self.window_seconds
            bucket = self._events.get(key)
            if bucket is None:
                self._ensure_capacity(cutoff)
                bucket = deque()
                self._events[key] = bucket
            else:
                self._events.move_to_end(key)
            self._prune_bucket(bucket, cutoff)

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

    @staticmethod
    def _prune_bucket(bucket: deque[float], cutoff: float) -> None:
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

    def _ensure_capacity(self, cutoff: float) -> None:
        if len(self._events) < self.max_buckets:
            return
        for key in list(self._events):
            bucket = self._events[key]
            self._prune_bucket(bucket, cutoff)
            if not bucket:
                del self._events[key]
            if len(self._events) < self.max_buckets:
                return
        while len(self._events) >= self.max_buckets:
            self._events.popitem(last=False)


def is_exempt_path(path: str, exempt_paths: list[str]) -> bool:
    return any(path == exempt or path.startswith(f"{exempt}/") for exempt in exempt_paths)


def build_rate_limit_key(
    *,
    operator_id_header: str,
    headers: dict | object,
    client_host: str | None,
    authenticated: bool = True,
) -> str:
    """Build the throttling bucket key for a request.

    `authenticated` must be False for any request that has not proven it holds
    the bearer token. Previously the key was derived from the `authorization`
    header unconditionally, which made brute force free: every guessed token
    hashed to its own fresh bucket, so an attacker never shared a counter with
    themselves and never hit the limit. An attacker-chosen operator-id header
    did the same, and thousands of distinct values also evicted every legitimate
    bucket from the LRU — a denial of service against the limiter itself.

    Unauthenticated traffic is therefore keyed by client IP only, which is the
    one identifier the caller cannot mint at will.
    """
    get = getattr(headers, "get", lambda *_args, **_kwargs: None)

    if not authenticated:
        return f"unauth-ip:{client_host or 'unknown'}"

    authorization = get("authorization")
    if authorization:
        token_hash = hashlib.sha256(str(authorization).encode("utf-8")).hexdigest()[:16]
        return f"auth:{token_hash}"
    operator_id = get(operator_id_header)
    if operator_id:
        operator_hash = hashlib.sha256(str(operator_id).strip().encode("utf-8")).hexdigest()[:16]
        return f"operator:{operator_hash}"
    return f"ip:{client_host or 'unknown'}"

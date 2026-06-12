from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    capacity: float
    tokens: float
    refill_per_sec: float
    last_refill: float


class TokenBucket:
    """In-memory token-bucket rate limiter keyed by string.

    Each key gets its own bucket lazily. Restart-safe by design: limits reset
    when the Space restarts (acceptable per the design's in-memory state model).
    """

    def __init__(self, capacity: int, refill_per_minute: int):
        if capacity <= 0 or refill_per_minute <= 0:
            raise ValueError("capacity and refill_per_minute must be positive")
        self._capacity = float(capacity)
        self._refill_per_sec = refill_per_minute / 60.0
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def _get(self, key: str, now: float) -> _Bucket:
        b = self._buckets.get(key)
        if b is None:
            b = _Bucket(
                capacity=self._capacity,
                tokens=self._capacity,
                refill_per_sec=self._refill_per_sec,
                last_refill=now,
            )
            self._buckets[key] = b
        return b

    def try_consume(self, key: str, n: int = 1) -> tuple[bool, int]:
        """Attempt to consume n tokens. Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        with self._lock:
            b = self._get(key, now)
            elapsed = now - b.last_refill
            b.tokens = min(b.capacity, b.tokens + elapsed * b.refill_per_sec)
            b.last_refill = now
            if b.tokens >= n:
                b.tokens -= n
                return True, 0
            deficit = n - b.tokens
            retry_after = max(1, int(deficit / b.refill_per_sec) + 1)
            return False, retry_after

    def refund(self, key: str, n: int = 1) -> None:
        """Return tokens consumed by a try_consume that was later rolled back."""
        with self._lock:
            b = self._buckets.get(key)
            if b is not None:
                b.tokens = min(b.capacity, b.tokens + n)


class CompoundLimiter:
    """Apply multiple TokenBuckets to the same key; the strictest wins.

    A rejection refunds the buckets that had already allowed the request, so
    hammering past one limit cannot also drain the others.
    """

    def __init__(self, *buckets: TokenBucket):
        self._buckets = buckets

    def try_consume(self, key: str, n: int = 1) -> tuple[bool, int]:
        consumed: list[TokenBucket] = []
        worst_retry = 0
        for b in self._buckets:
            allowed, retry = b.try_consume(key, n)
            if allowed:
                consumed.append(b)
            else:
                worst_retry = max(worst_retry, retry)
        if worst_retry > 0:
            for b in consumed:
                b.refund(key, n)
            return False, worst_retry
        return True, 0

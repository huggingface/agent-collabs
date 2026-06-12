"""TokenBucket / CompoundLimiter unit tests."""
from __future__ import annotations

from app.rate_limit import CompoundLimiter, TokenBucket


def test_token_bucket_consumes_then_rejects_with_retry():
    bucket = TokenBucket(capacity=2, refill_per_minute=1)
    assert bucket.try_consume("k") == (True, 0)
    assert bucket.try_consume("k") == (True, 0)
    allowed, retry = bucket.try_consume("k")
    assert allowed is False
    assert retry >= 1


def test_compound_rejection_refunds_buckets_that_allowed():
    burst = TokenBucket(capacity=3, refill_per_minute=1)
    sustained = TokenBucket(capacity=1, refill_per_minute=1)
    limiter = CompoundLimiter(burst, sustained)

    assert limiter.try_consume("k") == (True, 0)  # burst: 2 left, sustained: 0

    # Hammer past the sustained limit: every attempt is rejected, and each
    # rejection must refund the burst token it briefly took.
    for _ in range(10):
        allowed, retry = limiter.try_consume("k")
        assert allowed is False
        assert retry >= 1

    # The rejected attempts did not drain burst — its 2 remaining tokens are
    # still there.
    assert burst.try_consume("k")[0] is True
    assert burst.try_consume("k")[0] is True


def test_compound_keys_are_independent():
    limiter = CompoundLimiter(
        TokenBucket(capacity=1, refill_per_minute=1),
        TokenBucket(capacity=1, refill_per_minute=1),
    )
    assert limiter.try_consume("a")[0] is True
    assert limiter.try_consume("a")[0] is False
    assert limiter.try_consume("b")[0] is True

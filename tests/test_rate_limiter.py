from __future__ import annotations

from app.services.visits import TokenBucketRateLimiter


async def test_token_bucket_refills_without_fixed_window_boundary(monkeypatch) -> None:
    now = 100.0
    monkeypatch.setattr("app.services.visits.time.monotonic", lambda: now)
    limiter = TokenBucketRateLimiter()

    assert await limiter.check("visitor", limit=2, window_seconds=60) is None
    assert await limiter.check("visitor", limit=2, window_seconds=60) is None
    assert await limiter.check("visitor", limit=2, window_seconds=60) == 30

    now = 130.0
    assert await limiter.check("visitor", limit=2, window_seconds=60) is None


async def test_token_bucket_never_exceeds_maximum_entries() -> None:
    limiter = TokenBucketRateLimiter(max_items=3)
    for index in range(10):
        assert await limiter.check(f"visitor-{index}", limit=1) is None
    assert len(limiter._items) == 3

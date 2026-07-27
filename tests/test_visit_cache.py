from __future__ import annotations

from app.services.visits import ExpiringReservationCache


async def test_reservation_can_be_released_for_retry() -> None:
    cache = ExpiringReservationCache()
    assert await cache.reserve("visitor", 30) is True
    assert await cache.reserve("visitor", 30) is False
    await cache.release("visitor")
    assert await cache.reserve("visitor", 30) is True


async def test_commit_keeps_successful_visit_deduplicated() -> None:
    cache = ExpiringReservationCache()
    assert await cache.reserve("visitor", 1) is True
    await cache.commit("visitor", 30)
    assert await cache.reserve("visitor", 30) is False

from __future__ import annotations

import asyncio
import math
import time
from collections import OrderedDict


class TokenBucketRateLimiter:
    """Bounded in-memory token bucket.

    This is a process-local protection layer. Production deployments should
    also use provider/WAF rate limits when available.
    """

    def __init__(self, max_items: int = 10000) -> None:
        self.max_items = max_items
        self._items: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def check(self, key: str, limit: int, window_seconds: int = 60) -> int | None:
        now = time.monotonic()
        refill_rate = limit / window_seconds

        async with self._lock:
            tokens, updated_at = self._items.get(key, (float(limit), now))
            tokens = min(float(limit), tokens + max(0.0, now - updated_at) * refill_rate)

            if tokens >= 1.0:
                tokens -= 1.0
                retry_after = None
            else:
                retry_after = max(1, math.ceil((1.0 - tokens) / refill_rate))

            self._items[key] = (tokens, now)
            self._items.move_to_end(key)
            while len(self._items) > self.max_items:
                self._items.popitem(last=False)

            return retry_after

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class LeakyBucketRateLimiter:
    """
    Implements Shopify's REST API leaky bucket model.
    Bucket capacity: configurable (default 40).
    Refill rate: configurable (default 2 calls/sec).
    """

    def __init__(self, bucket_size: float = 40.0, refill_rate: float = 2.0) -> None:
        self._capacity = bucket_size
        self._refill_rate = refill_rate
        self._tokens = bucket_size
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_rate)
        self._last_refill = now

    async def acquire(self) -> None:
        async with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            wait = (1.0 - self._tokens) / self._refill_rate
            logger.debug(f"REST rate limiter: bucket empty, sleeping {wait:.2f}s")
            await asyncio.sleep(wait)
            self._refill()
            self._tokens -= 1.0

    def sync_from_header(self, header_value: str) -> None:
        """
        Sync bucket state from X-Shopify-Shop-Api-Call-Limit header.
        Format: "current/max" e.g. "38/40"
        """
        try:
            current, maximum = header_value.split("/")
            cap = float(maximum)
            used = float(current)
            self._capacity = cap
            self._tokens = max(0.0, cap - used)
        except Exception:
            pass


class GraphQLCostRateLimiter:
    """
    Tracks GraphQL query cost budget based on Shopify's throttleStatus.
    Sleeps before sending if available cost is below threshold.
    """

    def __init__(
        self,
        max_cost: float = 1000.0,
        restore_rate: float = 50.0,
        threshold: float = 200.0,
    ) -> None:
        self._max_cost = max_cost
        self._restore_rate = restore_rate
        self._threshold = threshold
        self._available = max_cost
        self._lock = asyncio.Lock()

    async def acquire(self, estimated_cost: float = 100.0) -> None:
        async with self._lock:
            if self._available >= estimated_cost:
                return
            wait = (estimated_cost - self._available) / self._restore_rate
            logger.debug(
                f"GraphQL cost limiter: available={self._available:.0f}, "
                f"need={estimated_cost:.0f}, sleeping {wait:.2f}s"
            )
            await asyncio.sleep(wait)
            self._available = min(self._max_cost, self._available + wait * self._restore_rate)

    def update(self, throttle_status: dict) -> None:
        """
        Called after each GraphQL response with extensions.cost.throttleStatus.
        """
        try:
            self._available = float(throttle_status.get("currentlyAvailable", self._available))
            self._restore_rate = float(throttle_status.get("restoreRate", self._restore_rate))
            self._max_cost = float(throttle_status.get("maximumAvailable", self._max_cost))
            if self._available < self._threshold:
                logger.debug(
                    f"GraphQL cost low: {self._available:.0f}/{self._max_cost:.0f} available"
                )
        except Exception:
            pass

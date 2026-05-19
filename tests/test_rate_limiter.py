from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from migration.rate_limiter import GraphQLCostRateLimiter, LeakyBucketRateLimiter


class TestLeakyBucketRateLimiter:
    async def test_acquire_when_tokens_available(self):
        limiter = LeakyBucketRateLimiter(bucket_size=10.0, refill_rate=2.0)
        # Should not sleep — bucket is full
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await limiter.acquire()
            mock_sleep.assert_not_called()

    async def test_acquire_drains_tokens(self):
        limiter = LeakyBucketRateLimiter(bucket_size=2.0, refill_rate=2.0)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await limiter.acquire()
            await limiter.acquire()
        # Tokens should be at 0 (minus refill during processing)
        assert limiter._tokens < 1.0

    async def test_acquire_sleeps_when_empty(self):
        limiter = LeakyBucketRateLimiter(bucket_size=1.0, refill_rate=1.0)
        limiter._tokens = 0.0

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with patch("time.monotonic", side_effect=[0.0, 0.0, 0.0, 1.0, 1.0]):
                await limiter.acquire()
            mock_sleep.assert_called_once()
            sleep_duration = mock_sleep.call_args[0][0]
            assert sleep_duration > 0

    def test_sync_from_header_updates_tokens(self):
        limiter = LeakyBucketRateLimiter(bucket_size=40.0, refill_rate=2.0)
        limiter.sync_from_header("35/40")
        assert limiter._capacity == 40.0
        assert limiter._tokens == 5.0  # 40 - 35 used

    def test_sync_from_header_ignores_malformed(self):
        limiter = LeakyBucketRateLimiter(bucket_size=40.0, refill_rate=2.0)
        original_tokens = limiter._tokens
        limiter.sync_from_header("bad-header")
        assert limiter._tokens == original_tokens


class TestGraphQLCostRateLimiter:
    async def test_acquire_when_available(self):
        limiter = GraphQLCostRateLimiter(max_cost=1000.0, restore_rate=50.0, threshold=200.0)
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await limiter.acquire(100.0)
            mock_sleep.assert_not_called()

    async def test_acquire_sleeps_when_insufficient(self):
        limiter = GraphQLCostRateLimiter(max_cost=1000.0, restore_rate=50.0, threshold=200.0)
        limiter._available = 50.0

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await limiter.acquire(estimated_cost=200.0)
            mock_sleep.assert_called_once()
            sleep_duration = mock_sleep.call_args[0][0]
            assert sleep_duration == pytest.approx(3.0)  # (200 - 50) / 50 = 3.0

    def test_update_sets_available(self):
        limiter = GraphQLCostRateLimiter()
        limiter.update({
            "currentlyAvailable": 750.0,
            "restoreRate": 50.0,
            "maximumAvailable": 1000.0,
        })
        assert limiter._available == 750.0
        assert limiter._restore_rate == 50.0
        assert limiter._max_cost == 1000.0

    def test_update_ignores_malformed(self):
        limiter = GraphQLCostRateLimiter()
        original = limiter._available
        limiter.update({"bad": "data"})
        assert limiter._available == original

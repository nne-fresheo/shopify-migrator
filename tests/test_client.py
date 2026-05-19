from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from migration.client import ShopifyClient


_DUMMY_REQUEST = httpx.Request("GET", "https://test.myshopify.com/admin/api/2024-01/products.json")


def _make_response(status: int, body: dict | None = None, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json=body or {},
        headers=headers or {},
        request=_DUMMY_REQUEST,
    )


class TestShopifyClientRetry:
    async def test_retries_on_429_with_retry_after(self):
        response_429 = _make_response(429, {}, {"Retry-After": "1"})
        response_ok = _make_response(200, {"products": []})

        call_count = 0

        async def fake_request(method, url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return response_429
            return response_ok

        async with ShopifyClient("test.myshopify.com", "token", max_retries=3) as client:
            with patch.object(client._http, "request", side_effect=fake_request):
                with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                    result = await client.get("products.json")

        assert call_count == 2
        mock_sleep.assert_called_once()
        assert result == {"products": []}

    async def test_raises_after_max_retries_on_429(self):
        response_429 = _make_response(429, {}, {"Retry-After": "1"})

        async def fake_request(method, url, **kwargs):
            return response_429

        async with ShopifyClient("test.myshopify.com", "token", max_retries=2) as client:
            with patch.object(client._http, "request", side_effect=fake_request):
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    with pytest.raises(httpx.HTTPStatusError):
                        await client.get("products.json")

    async def test_retries_on_5xx(self):
        response_500 = _make_response(500, {})
        response_ok = _make_response(200, {"pages": []})

        call_count = 0

        async def fake_request(method, url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return response_500
            return response_ok

        async with ShopifyClient("test.myshopify.com", "token", max_retries=3) as client:
            with patch.object(client._http, "request", side_effect=fake_request):
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    result = await client.get("pages.json")

        assert call_count == 2
        assert result == {"pages": []}

    async def test_does_not_retry_4xx(self):
        response_422 = _make_response(422, {"errors": "invalid"})

        call_count = 0

        async def fake_request(method, url, **kwargs):
            nonlocal call_count
            call_count += 1
            return response_422

        async with ShopifyClient("test.myshopify.com", "token", max_retries=3) as client:
            with patch.object(client._http, "request", side_effect=fake_request):
                with pytest.raises(httpx.HTTPStatusError):
                    await client.get("products.json")

        assert call_count == 1

    async def test_syncs_rate_limiter_from_header(self):
        response_ok = _make_response(
            200, {"products": []}, {"X-Shopify-Shop-Api-Call-Limit": "30/40"}
        )

        async def fake_request(method, url, **kwargs):
            return response_ok

        async with ShopifyClient("test.myshopify.com", "token") as client:
            with patch.object(client._http, "request", side_effect=fake_request):
                await client.get("products.json")

        assert client._rest_limiter._tokens == pytest.approx(10.0, abs=0.1)


class TestShopifyClientPagination:
    async def test_follows_next_link(self):
        page1 = _make_response(
            200,
            {"products": [{"id": 1}]},
            {"Link": '<https://test.myshopify.com/admin/api/2024-01/products.json?limit=250&page_info=abc>; rel="next"'},
        )
        page2 = _make_response(200, {"products": [{"id": 2}]})

        call_count = 0

        async def fake_request(method, url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return page1
            return page2

        async with ShopifyClient("test.myshopify.com", "token") as client:
            with patch.object(client._http, "request", side_effect=fake_request):
                pages = []
                async for page in client.get_paginated("products.json", "products"):
                    pages.append(page)

        assert call_count == 2
        assert pages == [[{"id": 1}], [{"id": 2}]]

    async def test_stops_when_no_next_link(self):
        response = _make_response(200, {"products": [{"id": 1}]})

        async def fake_request(method, url, **kwargs):
            return response

        async with ShopifyClient("test.myshopify.com", "token") as client:
            with patch.object(client._http, "request", side_effect=fake_request):
                pages = []
                async for page in client.get_paginated("products.json", "products"):
                    pages.append(page)

        assert len(pages) == 1

    async def test_yields_empty_list_on_empty_store(self):
        response = _make_response(200, {"products": []})

        async def fake_request(method, url, **kwargs):
            return response

        async with ShopifyClient("test.myshopify.com", "token") as client:
            with patch.object(client._http, "request", side_effect=fake_request):
                pages = []
                async for page in client.get_paginated("products.json", "products"):
                    pages.append(page)

        assert pages == [[]]

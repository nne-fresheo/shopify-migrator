from __future__ import annotations

import httpx
import pytest
import respx

from migration.menu_autofill.loop_client import LoopAdminClient, LoopStorefrontClient

BASE = "https://loop.test"
ADMIN = f"{BASE}/admin/2023-10"
STORE = f"{BASE}/storefront/2023-10"


def _admin() -> LoopAdminClient:
    return LoopAdminClient("admin-tok", base_url=BASE, api_version="2023-10", max_retries=1)


@respx.mock
async def test_iter_scheduled_orders_paginates_and_filters_status():
    respx.get(f"{ADMIN}/order/schedule/").mock(
        side_effect=[
            httpx.Response(200, json={
                "data": [
                    {"subscription": {"id": "s1"}, "status": "UNPROCESSED"},
                    {"subscription": {"id": "s2"}, "status": "PROCESSED"},  # filtered out
                ],
                "pageInfo": {"hasNextPage": True},
            }),
            httpx.Response(200, json={
                "data": [{"subscription": {"id": "s3"}, "status": "UNPROCESSED"}],
                "pageInfo": {"hasNextPage": False},
            }),
        ]
    )
    async with _admin() as admin:
        rows = [
            r async for r in admin.iter_scheduled_orders(
                billing_start_epoch=0, billing_end_epoch=10
            )
        ]
    ids = [r["subscription"]["id"] for r in rows]
    assert ids == ["s1", "s3"]


@respx.mock
async def test_get_subscription_unwraps_data():
    respx.get(f"{ADMIN}/subscription/s1").mock(
        return_value=httpx.Response(200, json={"data": {"id": "s1", "bundleTransactionId": "b1"}})
    )
    async with _admin() as admin:
        detail = await admin.get_subscription("s1")
    assert detail["bundleTransactionId"] == "b1"


@respx.mock
async def test_mint_session_token():
    route = respx.post(f"{ADMIN}/customer/c1/sessionToken").mock(
        return_value=httpx.Response(200, json={"sessionToken": "sess-xyz"})
    )
    async with _admin() as admin:
        token = await admin.mint_session_token("c1")
    assert token == "sess-xyz"
    assert route.called
    assert route.calls.last.request.headers["X-Loop-Token"] == "admin-tok"


@respx.mock
async def test_storefront_mints_and_caches_access_token():
    session = respx.post(f"{ADMIN}/customer/c1/sessionToken").mock(
        return_value=httpx.Response(200, json={"sessionToken": "sess"})
    )
    refresh = respx.post(f"{STORE}/auth/refreshToken").mock(
        return_value=httpx.Response(200, json={"accessToken": "acc", "refreshToken": "ref"})
    )
    tx = respx.get(f"{STORE}/bundle/transaction/b1").mock(
        return_value=httpx.Response(200, json={"data": {"items": [], "boxSizeId": "bx", "discountId": "d1"}})
    )

    async with _admin() as admin, LoopStorefrontClient(admin, base_url=BASE, max_retries=1) as sf:
        first = await sf.get_bundle_transaction("c1", "b1")
        second = await sf.get_bundle_transaction("c1", "b1")

    assert first["boxSizeId"] == "bx"
    assert second["discountId"] == "d1"
    # Token minted once and reused for the second read.
    assert session.call_count == 1
    assert refresh.call_count == 1
    assert tx.call_count == 2
    assert tx.calls.last.request.headers["Authorization"] == "Bearer acc"


@respx.mock
async def test_storefront_rotates_on_401_then_retries():
    respx.post(f"{ADMIN}/customer/c1/sessionToken").mock(
        return_value=httpx.Response(200, json={"sessionToken": "sess"})
    )
    respx.post(f"{STORE}/auth/refreshToken").mock(
        return_value=httpx.Response(200, json={"accessToken": "stale", "refreshToken": "ref"})
    )
    rotate = respx.post(f"{STORE}/auth/rotateToken").mock(
        return_value=httpx.Response(200, json={"accessToken": "fresh", "refreshToken": "ref2"})
    )
    # First call 401 (stale token), second call (fresh token) succeeds.
    respx.get(f"{STORE}/bundle/transaction/b1").mock(
        side_effect=[
            httpx.Response(401, json={"error": "expired"}),
            httpx.Response(200, json={"data": {"items": [], "boxSizeId": "bx"}}),
        ]
    )

    async with _admin() as admin, LoopStorefrontClient(admin, base_url=BASE, max_retries=1) as sf:
        result = await sf.get_bundle_transaction("c1", "b1")

    assert result["boxSizeId"] == "bx"
    assert rotate.call_count == 1


@respx.mock
async def test_update_bundle_sends_full_payload():
    respx.post(f"{ADMIN}/customer/c1/sessionToken").mock(
        return_value=httpx.Response(200, json={"sessionToken": "sess"})
    )
    respx.post(f"{STORE}/auth/refreshToken").mock(
        return_value=httpx.Response(200, json={"accessToken": "acc", "refreshToken": "ref"})
    )
    update = respx.post(f"{STORE}/bundle/transaction/update").mock(
        return_value=httpx.Response(200, json={"data": {"ok": True}})
    )

    items = [{"productVariantShopifyId": 9, "quantity": 2}]
    async with _admin() as admin, LoopStorefrontClient(admin, base_url=BASE, max_retries=1) as sf:
        await sf.update_bundle("c1", transaction_id="b1", items=items, discount_id="d1", box_size_id="bx")

    body = update.calls.last.request.read()
    import json
    payload = json.loads(body)
    assert payload == {"id": "b1", "items": items, "discountId": "d1", "boxSizeId": "bx"}

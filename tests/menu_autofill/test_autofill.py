from __future__ import annotations

import random
from unittest.mock import AsyncMock

import pytest

from migration.client import ShopifyClient
from migration.menu_autofill.autofill import MenuAutofiller, write_audit
from migration.menu_autofill.models import Decision

NOW = 1_000_000
HOUR = 3600


# ── builders ────────────────────────────────────────────────────────────────

def _prod(pid, tags, variants):
    return {
        "node": {
            "id": f"gid://shopify/Product/{pid}",
            "title": f"P{pid}",
            "tags": tags,
            "variants": {
                "edges": [
                    {"node": {"id": f"gid://shopify/ProductVariant/{vid}", "availableForSale": a}}
                    for vid, a in variants
                ]
            },
        }
    }


def _make_shopify(menu_products, product_tags=None):
    shopify = AsyncMock(spec=ShopifyClient)

    async def graphql(query, variables=None, estimated_cost=100.0):
        if "menuProducts" in query:
            return {"products": {"edges": menu_products, "pageInfo": {"hasNextPage": False}}}
        if "productTags" in query:
            pid = int(str(variables["id"]).rsplit("/", 1)[-1])
            return {"product": {"id": variables["id"], "tags": (product_tags or {}).get(pid, [])}}
        return {}

    shopify.graphql.side_effect = graphql
    return shopify


def _line(variant, product, qty=1, removed=False, added=False, title=""):
    return {
        "variantShopifyId": variant,
        "productShopifyId": product,
        "quantity": qty,
        "isOneTimeRemoved": removed,
        "isOneTimeAdded": added,
        "title": title or f"meal-{variant}",
    }


def _detail(tx_id, lines):
    return {"id": "s1", "bundleTransactionId": tx_id, "lineItems": lines}


def _sched(billing, sub_id="s1", cust="c1"):
    return {
        "subscription": {"id": sub_id},
        "customer": {"shopifyId": cust},
        "billingDateEpoch": billing,
    }


class _FakeAdmin:
    def __init__(self, rows, get_subscription):
        self._rows = rows
        self.get_subscription = get_subscription
        self.mint_session_token = AsyncMock(return_value="sess")

    async def iter_scheduled_orders(self, **kwargs):
        for r in self._rows:
            yield r


def _autofiller(shopify, admin, storefront=None, *, dry_run=True, seed=0, **kw):
    return MenuAutofiller(
        shopify=shopify,
        admin=admin,
        storefront=storefront or AsyncMock(),
        dry_run=dry_run,
        now_epoch=NOW,
        rng=random.Random(seed),
        **kw,
    )


# ── tests ─────────────────────────────────────────────────────────────────────

async def test_skip_when_all_in_menu():
    shopify = _make_shopify([_prod(1, ["current-menu", "main-dish"], [(11, True)])])
    admin = _FakeAdmin(
        [_sched(NOW + 40 * HOUR)],
        AsyncMock(return_value=_detail("b1", [_line(11, 1)])),
    )
    rows = await _autofiller(shopify, admin).run()
    assert len(rows) == 1
    assert rows[0].decision == Decision.SKIP.value
    assert rows[0].ok


async def test_adapt_dry_run_does_not_write():
    shopify = _make_shopify(
        [_prod(1, ["current-menu", "main-dish"], [(11, True), (12, True)])],
        product_tags={50: ["main-dish"]},
    )
    admin = _FakeAdmin(
        [_sched(NOW + 40 * HOUR)],
        AsyncMock(return_value=_detail("b1", [_line(5, 50, qty=2)])),  # stale main-dish
    )
    storefront = AsyncMock()
    rows = await _autofiller(shopify, admin, storefront).run()

    assert rows[0].decision == Decision.ADAPT.value
    assert rows[0].removed == [5]
    assert rows[0].added and rows[0].added[0] in (11, 12)
    storefront.update_bundle.assert_not_called()  # dry-run never writes


async def test_flag_when_no_same_category_candidate():
    shopify = _make_shopify(
        [_prod(1, ["current-menu", "main-dish"], [(11, True)])],
        product_tags={70: ["dessert"]},
    )
    admin = _FakeAdmin(
        [_sched(NOW + 40 * HOUR)],
        AsyncMock(return_value=_detail("b1", [_line(7, 70)])),  # stale dessert, no dessert in menu
    )
    rows = await _autofiller(shopify, admin).run()
    assert rows[0].decision == Decision.FLAG.value
    assert rows[0].ok is False
    assert rows[0].unswappable == [7]


async def test_locked_inside_min_lead():
    shopify = _make_shopify([_prod(1, ["current-menu", "main-dish"], [(11, True)])])
    admin = _FakeAdmin(
        [_sched(NOW + 10 * HOUR)],  # 10h < 24h lock window
        AsyncMock(return_value=_detail("b1", [_line(5, 50)])),
    )
    rows = await _autofiller(shopify, admin).run()
    assert rows[0].decision == Decision.LOCKED.value
    admin.get_subscription.assert_not_called()  # never even read the contract


async def test_no_bundle_when_transaction_id_missing():
    shopify = _make_shopify([_prod(1, ["current-menu", "main-dish"], [(11, True)])])
    admin = _FakeAdmin(
        [_sched(NOW + 40 * HOUR)],
        AsyncMock(return_value=_detail(None, [_line(11, 1)])),
    )
    rows = await _autofiller(shopify, admin).run()
    assert rows[0].decision == Decision.NO_BUNDLE.value


async def test_one_time_removed_meal_excluded_from_effective():
    # The only stale meal is flagged isOneTimeRemoved, so the box that actually
    # ships is all in-menu -> SKIP.
    shopify = _make_shopify(
        [_prod(1, ["current-menu", "main-dish"], [(11, True)])],
        product_tags={50: ["main-dish"]},
    )
    admin = _FakeAdmin(
        [_sched(NOW + 40 * HOUR)],
        AsyncMock(return_value=_detail("b1", [_line(11, 1), _line(5, 50, removed=True)])),
    )
    rows = await _autofiller(shopify, admin).run()
    assert rows[0].decision == Decision.SKIP.value


async def test_live_adapt_writes_and_verifies():
    shopify = _make_shopify(
        [_prod(1, ["current-menu", "main-dish"], [(9, True)])],
        product_tags={50: ["main-dish"]},
    )
    # First read: stale meal 5. Post-write read: in-menu meal 9 -> verify passes.
    get_subscription = AsyncMock(
        side_effect=[
            _detail("b1", [_line(5, 50)]),
            _detail("b1", [_line(9, 1)]),
        ]
    )
    admin = _FakeAdmin([_sched(NOW + 40 * HOUR)], get_subscription)
    storefront = AsyncMock()
    storefront.get_bundle_transaction = AsyncMock(
        return_value={"items": [], "boxSizeId": "bx", "discountId": "d1"}
    )
    storefront.update_bundle = AsyncMock(return_value={"ok": True})

    rows = await _autofiller(shopify, admin, storefront, dry_run=False).run()

    assert rows[0].decision == Decision.ADAPT.value
    assert rows[0].ok is True
    storefront.update_bundle.assert_awaited_once()
    kwargs = storefront.update_bundle.await_args.kwargs
    assert kwargs["transaction_id"] == "b1"
    assert kwargs["box_size_id"] == "bx"
    assert kwargs["discount_id"] == "d1"
    assert kwargs["items"] == [{"productVariantShopifyId": 9, "quantity": 1}]


async def test_live_adapt_verification_failure_marks_not_ok():
    shopify = _make_shopify(
        [_prod(1, ["current-menu", "main-dish"], [(9, True)])],
        product_tags={50: ["main-dish"]},
    )
    # Post-write read still shows the stale meal -> verification fails.
    get_subscription = AsyncMock(
        side_effect=[
            _detail("b1", [_line(5, 50)]),
            _detail("b1", [_line(5, 50)]),
        ]
    )
    admin = _FakeAdmin([_sched(NOW + 40 * HOUR)], get_subscription)
    storefront = AsyncMock()
    storefront.get_bundle_transaction = AsyncMock(return_value={"boxSizeId": "bx", "discountId": "d1"})
    storefront.update_bundle = AsyncMock(return_value={"ok": True})

    rows = await _autofiller(shopify, admin, storefront, dry_run=False).run()
    assert rows[0].decision == Decision.ADAPT.value
    assert rows[0].ok is False
    assert "verification failed" in rows[0].error


async def test_empty_menu_aborts():
    shopify = _make_shopify([])  # nothing tagged current-menu
    admin = _FakeAdmin([], AsyncMock())
    with pytest.raises(RuntimeError, match="Active menu is empty"):
        await _autofiller(shopify, admin).run()


async def test_limit_caps_processing():
    shopify = _make_shopify([_prod(1, ["current-menu", "main-dish"], [(11, True)])])
    admin = _FakeAdmin(
        [_sched(NOW + 40 * HOUR, sub_id=f"s{i}") for i in range(5)],
        AsyncMock(return_value=_detail("b1", [_line(11, 1)])),
    )
    rows = await _autofiller(shopify, admin).run(limit=2)
    assert len(rows) == 2


async def test_write_audit_csv(tmp_path):
    shopify = _make_shopify(
        [_prod(1, ["current-menu", "main-dish"], [(11, True), (12, True)])],
        product_tags={50: ["main-dish"]},
    )
    admin = _FakeAdmin(
        [_sched(NOW + 40 * HOUR)],
        AsyncMock(return_value=_detail("b1", [_line(5, 50)])),
    )
    rows = await _autofiller(shopify, admin).run()
    out = tmp_path / "audit.csv"
    write_audit(rows, out)
    text = out.read_text(encoding="utf-8")
    assert "subscription_id" in text.splitlines()[0]
    assert "adapt" in text
    assert "5->main-dish" in text  # category_mapping serialized (keyed by stale variant id)

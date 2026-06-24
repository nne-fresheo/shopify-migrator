from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from migration.client import ShopifyClient
from migration.menu_autofill.menu import build_active_menu, fetch_product_category


def _product_node(pid, tags, variants):
    return {
        "node": {
            "id": f"gid://shopify/Product/{pid}",
            "title": f"Product {pid}",
            "tags": tags,
            "variants": {
                "edges": [
                    {
                        "node": {
                            "id": f"gid://shopify/ProductVariant/{vid}",
                            "availableForSale": avail,
                        }
                    }
                    for vid, avail in variants
                ]
            },
        }
    }


@pytest.fixture
def shopify() -> AsyncMock:
    return AsyncMock(spec=ShopifyClient)


async def test_build_active_menu_groups_by_category_and_excludes_oos(shopify):
    shopify.graphql.return_value = {
        "products": {
            "edges": [
                _product_node(1, ["current-menu", "main-dish", "meat", "nutri-a"],
                              [(11, True), (12, False)]),
                _product_node(2, ["current-menu", "dessert"], [(21, True)]),
                _product_node(3, ["current-menu", "main-dish"], [(31, True)]),
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    }

    menu = await build_active_menu(shopify, active_menu_tag="current-menu")

    # 12 is out of stock -> excluded from the sellable set and candidate pools.
    assert menu.active_variant_ids == {11, 21, 31}
    assert menu.variant_to_category == {11: "main-dish", 12: "main-dish", 21: "dessert", 31: "main-dish"}
    assert {m.variant_id for m in menu.meals_by_category["main-dish"]} == {11, 31}
    assert {m.variant_id for m in menu.meals_by_category["dessert"]} == {21}
    assert menu.category_counts() == {"dessert": 1, "main-dish": 2}


async def test_build_active_menu_paginates(shopify):
    pages = [
        {
            "products": {
                "edges": [_product_node(1, ["current-menu", "main-dish"], [(11, True)])],
                "pageInfo": {"hasNextPage": True, "endCursor": "CUR"},
            }
        },
        {
            "products": {
                "edges": [_product_node(2, ["current-menu", "dessert"], [(21, True)])],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        },
    ]
    shopify.graphql.side_effect = pages
    menu = await build_active_menu(shopify)
    assert menu.active_variant_ids == {11, 21}
    assert shopify.graphql.await_count == 2


async def test_product_without_category_tag_still_sellable_but_no_candidate(shopify):
    shopify.graphql.return_value = {
        "products": {
            "edges": [_product_node(1, ["current-menu", "nutri-b"], [(11, True)])],
            "pageInfo": {"hasNextPage": False},
        }
    }
    menu = await build_active_menu(shopify)
    assert menu.active_variant_ids == {11}        # counts as in-menu
    assert menu.meals_by_category == {}            # but not a swap candidate


async def test_fetch_product_category(shopify):
    shopify.graphql.return_value = {
        "product": {"id": "gid://shopify/Product/5", "tags": ["dessert", "current-menu"]}
    }
    assert await fetch_product_category(shopify, 5) == "dessert"

    shopify.graphql.return_value = {"product": None}
    assert await fetch_product_category(shopify, 99) is None

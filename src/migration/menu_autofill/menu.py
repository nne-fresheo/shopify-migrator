from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from ..client import ShopifyClient
from .models import MenuMeal, category_from_tags

logger = logging.getLogger(__name__)

# Active-menu products, their variants, tags and sellability. `availableForSale`
# already folds in inventory level and the variant's inventory policy, so it is
# the single source of truth for "can ship this week". We still request the tags
# to recover each meal's category.
_GQL_MENU_PRODUCTS = """
query menuProducts($query: String!, $cursor: String) {
  products(first: 50, after: $cursor, query: $query) {
    edges {
      node {
        id
        title
        tags
        variants(first: 100) {
          edges {
            node {
              id
              availableForSale
            }
          }
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

# Tags of a single product, by id — used to recover the category of a *stale*
# meal that is no longer in the active menu.
_GQL_PRODUCT_TAGS = """
query productTags($id: ID!) {
  product(id: $id) {
    id
    tags
  }
}
"""


def _gid_to_int(gid: str) -> int:
    """'gid://shopify/ProductVariant/123' -> 123."""
    return int(gid.rsplit("/", 1)[-1])


@dataclass
class ActiveMenu:
    """The week's sellable menu, indexed for fast lookups.

    - ``active_variant_ids``: variants that are *in the current menu and in
      stock*. A bundle meal whose variant is not in this set is "stale".
    - ``meals_by_category``: in-stock candidates grouped by category (the swap
      pool).
    - ``variant_to_category`` / ``variant_to_product``: lookups for variants in
      the active menu.
    """

    active_variant_ids: set[int] = field(default_factory=set)
    meals_by_category: dict[str, list[MenuMeal]] = field(default_factory=dict)
    variant_to_category: dict[int, str] = field(default_factory=dict)
    variant_to_product: dict[int, int] = field(default_factory=dict)
    by_variant: dict[int, MenuMeal] = field(default_factory=dict)

    def category_counts(self) -> dict[str, int]:
        return {cat: len(meals) for cat, meals in sorted(self.meals_by_category.items())}


async def build_active_menu(
    client: ShopifyClient,
    *,
    active_menu_tag: str = "current-menu",
) -> ActiveMenu:
    """Build the active menu from Shopify products carrying ``active_menu_tag``.

    Out-of-stock variants are recorded but excluded from both
    ``active_variant_ids`` and the per-category candidate pools, so the planner
    never keeps nor proposes a meal that cannot ship.
    """
    menu = ActiveMenu()
    cursor: Optional[str] = None
    query = f"tag:'{active_menu_tag}'"
    product_count = 0

    while True:
        data = await client.graphql(
            _GQL_MENU_PRODUCTS,
            variables={"query": query, "cursor": cursor},
            estimated_cost=120.0,
        )
        block = data.get("products", {})
        for edge in block.get("edges", []):
            node = edge["node"]
            product_count += 1
            product_id = _gid_to_int(node["id"])
            category = category_from_tags(node.get("tags", []))
            for vedge in node.get("variants", {}).get("edges", []):
                v = vedge["node"]
                variant_id = _gid_to_int(v["id"])
                in_stock = bool(v.get("availableForSale", False))
                meal = MenuMeal(
                    product_id=product_id,
                    variant_id=variant_id,
                    title=node.get("title", ""),
                    category=category,
                    in_stock=in_stock,
                )
                menu.by_variant[variant_id] = meal
                menu.variant_to_product[variant_id] = product_id
                if category:
                    menu.variant_to_category[variant_id] = category
                if not in_stock:
                    continue
                menu.active_variant_ids.add(variant_id)
                if category:
                    menu.meals_by_category.setdefault(category, []).append(meal)

        page = block.get("pageInfo", {})
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")

    logger.info(
        "[menu] built active menu: %d products, %d sellable variants, categories=%s",
        product_count,
        len(menu.active_variant_ids),
        menu.category_counts(),
    )
    if product_count == 0:
        logger.warning(
            "[menu] no products carry tag %r — every bundle would look stale. "
            "Refusing to treat an empty menu as authoritative.",
            active_menu_tag,
        )
    return menu


async def fetch_product_category(
    client: ShopifyClient, product_id: int
) -> Optional[str]:
    """Resolve a single product's category from its Shopify tags.

    Used for *stale* meals whose product is no longer in the active menu. Returns
    None when the product is gone or carries no category tag.
    """
    gid = f"gid://shopify/Product/{product_id}"
    data = await client.graphql(
        _GQL_PRODUCT_TAGS, variables={"id": gid}, estimated_cost=10.0
    )
    node = data.get("product")
    if not node:
        return None
    return category_from_tags(node.get("tags", []))

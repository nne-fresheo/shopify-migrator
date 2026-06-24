from __future__ import annotations

import logging
import random
from typing import Optional

from .menu import ActiveMenu
from .models import BundleMeal, Decision, PlanResult, Swap

logger = logging.getLogger(__name__)


def plan_bundle(
    effective_meals: list[BundleMeal],
    menu: ActiveMenu,
    *,
    rng: Optional[random.Random] = None,
) -> PlanResult:
    """Decide skip / adapt / flag for one subscription's next-order bundle.

    Pure and deterministic given ``rng``. Implements the locked core rule:

    - If **every** meal is in the current menu -> ``SKIP``.
    - Otherwise, for each **stale** meal (variant not in the active menu) pick a
      replacement of the **same category** and **same quantity**, excluding meals
      already in the box (valid kept meals + replacements chosen so far) and
      out-of-stock meals.
    - Per the maintainer's decision, there is **no default-category fallback**:
      if any stale meal has no same-category candidate, the whole bundle is
      ``FLAG``\\ ged for manual review and **not written** (a partially-fixed box
      would still ship a stale meal and fail the post-write "subset of menu"
      assertion).

    Item count is preserved (one replacement per stale line, same quantity), so
    the bundle's ``boxSizeId`` stays valid.
    """
    rng = rng or random.Random()

    stale = [m for m in effective_meals if m.variant_id not in menu.active_variant_ids]
    kept_valid = [
        m.variant_id for m in effective_meals if m.variant_id in menu.active_variant_ids
    ]

    if not stale:
        return PlanResult(decision=Decision.SKIP, kept_valid=kept_valid)

    # Seed the exclusion set with meals already valid in the box so a stale meal
    # is never replaced by a duplicate of one the subscriber keeps.
    used: set[int] = set(kept_valid)
    swaps: list[Swap] = []
    unswappable: list[BundleMeal] = []
    # Preserve the original line order; dict keeps insertion order and dedupes.
    new_items: "dict[int, int]" = {}

    for meal in effective_meals:
        if meal.variant_id in menu.active_variant_ids:
            new_items[meal.variant_id] = new_items.get(meal.variant_id, 0) + meal.quantity
            continue

        candidates = (
            [
                c
                for c in menu.meals_by_category.get(meal.category, [])
                if c.in_stock and c.variant_id not in used
            ]
            if meal.category
            else []
        )

        if not candidates:
            unswappable.append(meal)
            swaps.append(
                Swap(
                    category=meal.category,
                    quantity=meal.quantity,
                    removed_variant_id=meal.variant_id,
                    removed_title=meal.title,
                )
            )
            # Keep the stale meal in place for reporting; the bundle won't be
            # written when any meal is unswappable.
            new_items[meal.variant_id] = new_items.get(meal.variant_id, 0) + meal.quantity
            continue

        # Stable, reproducible pick: sort candidates then choose with rng.
        ordered = sorted(candidates, key=lambda c: c.variant_id)
        choice = rng.choice(ordered)
        used.add(choice.variant_id)
        swaps.append(
            Swap(
                category=meal.category,
                quantity=meal.quantity,
                removed_variant_id=meal.variant_id,
                removed_title=meal.title,
                added_variant_id=choice.variant_id,
                added_title=choice.title,
            )
        )
        new_items[choice.variant_id] = new_items.get(choice.variant_id, 0) + meal.quantity

    decision = Decision.FLAG if unswappable else Decision.ADAPT
    items = [
        {"productVariantShopifyId": vid, "quantity": qty}
        for vid, qty in new_items.items()
    ]
    return PlanResult(
        decision=decision,
        new_items=items,
        swaps=swaps,
        unswappable=unswappable,
        kept_valid=kept_valid,
    )

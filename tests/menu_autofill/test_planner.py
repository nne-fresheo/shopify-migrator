from __future__ import annotations

import random

from migration.menu_autofill.menu import ActiveMenu
from migration.menu_autofill.models import BundleMeal, Decision, MenuMeal
from migration.menu_autofill.planner import plan_bundle


def _menu(*meals: MenuMeal) -> ActiveMenu:
    """Build an ActiveMenu from in-stock candidate meals (as the builder would)."""
    menu = ActiveMenu()
    for m in meals:
        menu.by_variant[m.variant_id] = m
        menu.variant_to_product[m.variant_id] = m.product_id
        if m.category:
            menu.variant_to_category[m.variant_id] = m.category
        if m.in_stock:
            menu.active_variant_ids.add(m.variant_id)
            if m.category:
                menu.meals_by_category.setdefault(m.category, []).append(m)
    return menu


def _mm(variant_id, category, *, product_id=None, in_stock=True, title="") -> MenuMeal:
    return MenuMeal(
        product_id=product_id if product_id is not None else variant_id * 10,
        variant_id=variant_id,
        title=title or f"meal-{variant_id}",
        category=category,
        in_stock=in_stock,
    )


def _bm(variant_id, category, *, quantity=1, product_id=None, title="") -> BundleMeal:
    return BundleMeal(
        variant_id=variant_id,
        quantity=quantity,
        product_id=product_id,
        title=title or f"bundle-{variant_id}",
        category=category,
    )


def test_skip_when_all_meals_in_menu():
    menu = _menu(_mm(1, "main-dish"), _mm(2, "dessert"))
    result = plan_bundle([_bm(1, "main-dish"), _bm(2, "dessert")], menu)
    assert result.decision is Decision.SKIP
    assert result.new_items == []
    assert sorted(result.kept_valid) == [1, 2]


def test_adapt_swaps_only_the_stale_meal():
    menu = _menu(_mm(1, "main-dish"), _mm(9, "main-dish"), _mm(2, "dessert"))
    # variant 5 is stale (not in menu) but is a main-dish; 1 and 2 are valid.
    effective = [_bm(1, "main-dish"), _bm(5, "main-dish", quantity=2), _bm(2, "dessert")]
    result = plan_bundle(effective, menu, rng=random.Random(0))

    assert result.decision is Decision.ADAPT
    assert result.removed == [5]
    assert result.added == [9]  # only same-category candidate left
    # valid meals untouched, replacement keeps the stale meal's quantity (2).
    items = {it["productVariantShopifyId"]: it["quantity"] for it in result.new_items}
    assert items == {1: 1, 9: 2, 2: 1}
    assert len(result.new_items) == 3  # item count preserved -> boxSize stays valid


def test_flag_when_no_same_category_candidate():
    # Stale dessert, but the menu has no dessert candidate at all.
    menu = _menu(_mm(1, "main-dish"), _mm(2, "main-dish"))
    result = plan_bundle([_bm(1, "main-dish"), _bm(7, "dessert")], menu)

    assert result.decision is Decision.FLAG
    assert [m.variant_id for m in result.unswappable] == [7]
    # No write happens on FLAG; added is empty.
    assert result.added == []


def test_no_default_category_fallback():
    """Confirms the maintainer decision: never substitute across categories."""
    menu = _menu(_mm(1, "main-dish"), _mm(2, "main-dish"), _mm(3, "main-dish"))
    # Stale snack with plenty of main-dish candidates available.
    result = plan_bundle([_bm(8, "snack")], menu)
    assert result.decision is Decision.FLAG
    assert result.added == []


def test_replacement_excludes_meals_already_in_box():
    # Two main-dish candidates, but one (1) is already kept in the box, so the
    # stale meal must take the other (9), never a duplicate of 1.
    menu = _menu(_mm(1, "main-dish"), _mm(9, "main-dish"))
    result = plan_bundle([_bm(1, "main-dish"), _bm(5, "main-dish")], menu, rng=random.Random(1))
    assert result.decision is Decision.ADAPT
    assert result.added == [9]
    variants = [it["productVariantShopifyId"] for it in result.new_items]
    assert sorted(variants) == [1, 9]
    assert len(variants) == len(set(variants))  # no duplicates


def test_two_stale_same_category_get_distinct_replacements():
    menu = _menu(_mm(10, "main-dish"), _mm(11, "main-dish"), _mm(12, "main-dish"))
    result = plan_bundle([_bm(5, "main-dish"), _bm(6, "main-dish")], menu, rng=random.Random(3))
    assert result.decision is Decision.ADAPT
    assert len(result.added) == 2
    assert len(set(result.added)) == 2  # distinct


def test_two_stale_same_category_one_candidate_flags():
    # Only one candidate for two stale meals of the same category -> can't fill both.
    menu = _menu(_mm(10, "main-dish"))
    result = plan_bundle([_bm(5, "main-dish"), _bm(6, "main-dish")], menu, rng=random.Random(3))
    assert result.decision is Decision.FLAG
    assert len(result.unswappable) == 1


def test_out_of_stock_candidate_not_chosen():
    # Out-of-stock main-dish (9) is excluded; only in-stock (3) is eligible.
    oos = _mm(9, "main-dish", in_stock=False)
    menu = _menu(oos, _mm(3, "main-dish"))
    result = plan_bundle([_bm(5, "main-dish")], menu, rng=random.Random(0))
    assert result.decision is Decision.ADAPT
    assert result.added == [3]


def test_in_menu_but_out_of_stock_meal_is_stale():
    # Variant 9 is tagged current-menu but out of stock -> not in active_variant_ids
    # -> treated as stale and swapped for the in-stock candidate (3).
    menu = _menu(_mm(9, "main-dish", in_stock=False), _mm(3, "main-dish"))
    result = plan_bundle([_bm(9, "main-dish")], menu, rng=random.Random(0))
    assert result.decision is Decision.ADAPT
    assert result.removed == [9]
    assert result.added == [3]


def test_stale_meal_with_unknown_category_flags():
    menu = _menu(_mm(1, "main-dish"))
    result = plan_bundle([_bm(7, None)], menu)
    assert result.decision is Decision.FLAG
    assert [m.variant_id for m in result.unswappable] == [7]


def test_deterministic_with_seed():
    menu = _menu(*[_mm(v, "main-dish") for v in range(100, 110)])
    a = plan_bundle([_bm(5, "main-dish")], menu, rng=random.Random(42))
    b = plan_bundle([_bm(5, "main-dish")], menu, rng=random.Random(42))
    assert a.added == b.added

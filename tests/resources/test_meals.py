from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from migration.django_db import (
    _build_cooking,
    _build_recipe_flags,
    _build_tags,
    _build_variants,
    _extract_locale,
    _format_price,
    _is_active_today,
    _meal_type_tag,
    _parse_diet_flags,
    _percent_encode_url,
    _plan_label,
    _resolve_image_urls,
    category_label_fr,
    category_taxonomy_gid,
)
from migration.id_map import IDMap
from migration.resources.meals import (
    MealsResource,
    _build_managed_metafields,
    _canonical_metafield_value,
    _diff_images,
    _diff_metafields,
    _diff_product,
    _diff_publications,
    _escape_for_query,
    _match_reference,
    _needs_subscription_association,
    _needs_tracking_disable,
    _normalize_reference_token,
    _nutrition_payload,
    _parse_image_sources,
    _parse_tags_csv,
    _serialize_image_sources,
    _slugify,
    _variants_differ,
)
from migration.template import DescriptionRenderer


REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_PATH = REPO_ROOT / "templates" / "product_description.html.j2"


@pytest.fixture
def renderer() -> DescriptionRenderer:
    return DescriptionRenderer(TEMPLATE_PATH)


@pytest.fixture
def sample_meal() -> dict:
    return {
        "meal_id": 42,
        "name": "Poulet & légumes du soleil",
        "ingredients": "Poulet, tomates, courgette.",
        "allergens": "",
        "nutri_score": "A",
        "diets": {"vegetarien": False, "meat": True, "sans-gluten": True,
                  "sans-lactose": False, "sans-porc": False, "vegan": False,
                  "fish": False, "fitness": False},
        "diet_labels": {
            "vegetarien": "🥗 Végétarien", "vegan": "🌱 Vegan",
            "sans-gluten": "🌾 Sans gluten", "sans-lactose": "🥛 Sans lactose",
            "sans-porc": "🐷 Sans porc", "meat": "🥩 Viande",
            "fish": "🐟 Poisson", "fitness": "💪 Fitness",
        },
        "weight": 450,
        "kilo_calories": 254,
        "proteins": 32.54,
        "carbohydrates": 14.91,
        "lipids": 6.43,
        "sugars": 3.3,
        "saturated": 1.61,
        "fibers": 5.72,
        "salts": 0.93,
        "avg_rating": 4.7,
        "rating_count": 42,
        "image_urls": ["https://cdn.example.com/meal-42.jpg"],
        "unit_price": "9.50",
        "category": "MAIN_DISH",
        "category_label": "Repas",
        "category_gid": "gid://shopify/TaxonomyCategory/fb-2-15-2",
        "is_active_today": True,
        "tags": ["current-menu", "main-dish", "meat", "nutri-a", "sans-gluten"],
        "options": [{"name": "Size", "values": ["Standard", "Large"]}],
        "variants": [
            {"price": "9.50",  "option1": "Standard", "sku": "fresheo-42-1"},
            {"price": "11.50", "option1": "Large",    "sku": "fresheo-42-2"},
        ],
    }


_DEFAULT_PUBLICATIONS_RESPONSE = {"publications": {"edges": [
    {"node": {"id": "gid://shopify/Publication/1", "name": "Online Store"}},
    {"node": {"id": "gid://shopify/Publication/2", "name": "POS"}},
]}}


def _existing_node(
    payload: dict | None = None,
    *,
    legacy_id: str = "9001",
    body_html: str | None = None,
    title: str | None = None,
    tags: list[str] | None = None,
    product_type: str | None = None,
    options: list[dict] | None = None,
    variants_shape: list[dict] | None = None,
    image_ids: list[str] | None = None,
    image_sources: list[str] | None = None,
    publications: dict[str, bool] | None = None,
    selling_plan_group_ids: list[str] | None = None,
    category_gid: str | None = None,
    managed_metafields: dict[str, str] | None = None,
) -> dict:
    """Build a Shopify-shaped product node for `getProductById` / `findByTitle`
    responses. If `payload` is given, fields default to values that produce
    NO diff against that payload; pass overrides to introduce intentional drift.

    `managed_metafields` maps 'namespace.key' → raw value string for the managed
    metafield aliases (fresheo.*/shopify.*); absent keys come back as None."""
    payload = payload or {}

    final_title = payload.get("title", "") if title is None else title
    final_body = payload.get("body_html", "") if body_html is None else body_html
    final_pt = payload.get("product_type", "") if product_type is None else product_type
    final_tags = (
        sorted(_parse_tags_csv(payload.get("tags", "")))
        if tags is None else tags
    )
    final_options = (
        payload.get("options", []) if options is None else options
    )
    final_variants = (
        payload.get("variants", []) if variants_shape is None else variants_shape
    )
    final_image_ids = [] if image_ids is None else image_ids
    if image_sources is None:
        mf = payload.get("metafields") or []
        metafield_value = mf[0]["value"] if mf else ""
    else:
        metafield_value = _serialize_image_sources(image_sources)
    final_publications = (
        {"gid://shopify/Publication/1": True, "gid://shopify/Publication/2": True}
        if publications is None else publications
    )
    final_spg_ids = (
        ["gid://shopify/SellingPlanGroup/500"]
        if selling_plan_group_ids is None else selling_plan_group_ids
    )

    variant_edges = []
    for i, v in enumerate(final_variants):
        tracked = v.get("inventory_management") == "shopify"
        sel = (
            [{"name": "Size", "value": v["option1"]}]
            if v.get("option1") is not None else []
        )
        variant_edges.append({"node": {
            "id": f"gid://shopify/ProductVariant/{5001 + i}",
            "legacyResourceId": str(5001 + i),
            "sku": v.get("sku") or "",
            "price": v.get("price"),
            "inventoryItem": {"tracked": tracked},
            "selectedOptions": sel,
        }})

    return {
        "id": f"gid://shopify/Product/{legacy_id}",
        "legacyResourceId": legacy_id,
        "title": final_title,
        "bodyHtml": final_body,
        "tags": list(final_tags),
        "productType": final_pt,
        "category": {"id": category_gid} if category_gid else None,
        "options": [
            {"id": f"gid://{i}", "name": o["name"], "position": i+1,
             "values": list(o.get("values") or [])}
            for i, o in enumerate(final_options)
        ],
        "variants": {"edges": variant_edges},
        "images": {"edges": [
            {"node": {"id": f"gid://shopify/ProductImage/{img}"}}
            for img in final_image_ids
        ]},
        "metafield": {"value": metafield_value} if metafield_value else None,
        **{
            alias: ({"value": (managed_metafields or {}).get(full_key)}
                    if (managed_metafields or {}).get(full_key) is not None else None)
            for alias, full_key in (
                ("mf_nutri_score", "fresheo.nutri_score"),
                ("mf_nutrition", "fresheo.nutrition"),
                ("mf_cooking", "fresheo.cooking_instructions"),
                ("mf_author", "fresheo.author"),
                ("mf_allergens", "shopify.allergen-information"),
                ("mf_diets", "shopify.dietary-preferences"),
            )
        },
        "resourcePublicationsV2": {"edges": [
            {"node": {"publication": {"id": gid}, "isPublished": pub}}
            for gid, pub in final_publications.items()
        ]},
        "sellingPlanGroups": {"edges": [
            {"node": {"id": gid}} for gid in final_spg_ids
        ]},
    }


def _smart_graphql(
    title_search_response=None,
    *,
    selling_plan_group_response=None,
    get_product_response=None,
    products_by_tag_edges=None,
    metafield_definition_response=None,
    metaobject_definition_response=None,
    metaobjects_response=None,
):
    """Build a graphql side_effect that routes by query string. Handles the
    queries MealsResource issues per meal: listPublications, title-search,
    get-by-id, publish/unpublish, the subscription group lookup, the
    SPG-add mutation, and the post-load `productsByTag` reconciliation sweep.

    By default `get_product_response` is None — the by-id query returns
    `{"product": None}`, which combined with an empty title_search_response
    matches the "no existing product" path. Tests exercising the update path
    pass an `_existing_node(...)` as `get_product_response`.

    `products_by_tag_edges` seeds the reconciliation sweep — a list of
    {id, legacyResourceId, title, tags} node dicts; defaults to empty (no
    products carry the tag, so the sweep is a no-op).
    """
    title_search_response = title_search_response or {"products": {"edges": []}}
    selling_plan_group_response = selling_plan_group_response or {
        "sellingPlanGroups": {"edges": [{
            "node": {
                "id": "gid://shopify/SellingPlanGroup/500",
                "name": "Main Bundle",
                "merchantCode": "main-bundle",
            },
        }]}
    }

    async def graphql(query, variables=None, estimated_cost=0):
        if "listPublications" in query:
            return _DEFAULT_PUBLICATIONS_RESPONSE
        if "publishablePublish" in query:
            return {"publishablePublish": {"userErrors": []}}
        if "publishableUnpublish" in query:
            return {"publishableUnpublish": {"userErrors": []}}
        if "findSellingPlanGroup" in query:
            return selling_plan_group_response
        if "sellingPlanGroupAddProducts" in query:
            return {"sellingPlanGroupAddProducts": {
                "sellingPlanGroup": {"id": variables.get("id")},
                "userErrors": [],
            }}
        if "disableTracking" in query or "productVariantsBulkUpdate" in query:
            return {"productVariantsBulkUpdate": {"userErrors": []}}
        if "setCategory" in query or "productUpdate" in query:
            product = (variables or {}).get("product", {})
            return {"productUpdate": {
                "product": {
                    "id": product.get("id"),
                    "category": {"id": product.get("category")},
                },
                "userErrors": [],
            }}
        if "setMetafields" in query or "metafieldsSet" in query:
            return {"metafieldsSet": {"metafields": [], "userErrors": []}}
        if "mfDef" in query:
            # Default: no definition → native reference metafields are skipped.
            return metafield_definition_response or {
                "metafieldDefinitions": {"edges": []}
            }
        if "moDef" in query:
            return metaobject_definition_response or {"metaobjectDefinition": None}
        if "moList" in query:
            return metaobjects_response or {"metaobjects": {
                "edges": [], "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}
        if "getProductById" in query:
            return {"product": get_product_response}
        if "findByTitle" in query:
            return title_search_response
        if "productsByTag" in query:
            return {"products": {
                "edges": [{"node": n} for n in (products_by_tag_edges or [])],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}
        raise AssertionError(f"Unhandled GraphQL query:\n{query}")

    return graphql


def _make_resource(
    dest, tmp_data_dir, progress, failed_log, renderer,
    dry_run=False, subscription_group_code="main-bundle",
):
    return MealsResource(
        source_client=None,
        dest_client=dest,
        data_dir=tmp_data_dir,
        id_map=IDMap(tmp_data_dir / "id_maps" / "meals.json"),
        progress=progress,
        failed_log=failed_log,
        dry_run=dry_run,
        renderer=renderer,
        django_dsn="postgres://unused",
        subscription_group_code=subscription_group_code,
    )


# ── Helpers (django_db) ──────────────────────────────────────────────────────

class TestExtractLocale:
    def test_returns_fr_content(self):
        assert _extract_locale("<fr>Bonjour</fr><nl>Hallo</nl>", "fr") == "Bonjour"

    def test_returns_raw_when_no_tags(self):
        assert _extract_locale("Plain text", "fr") == "Plain text"

    def test_joins_multiple_matches(self):
        assert _extract_locale("<fr>Un</fr> sep <fr>Deux</fr>", "fr") == "Un Deux"

    def test_returns_empty_for_none(self):
        assert _extract_locale(None, "fr") == ""


class TestParseDietFlags:
    def test_maps_atoms_to_slugs(self):
        flags = _parse_diet_flags("vegetarian gluten_free meat")
        assert flags["vegetarien"] is True
        assert flags["sans-gluten"] is True
        assert flags["meat"] is True
        assert flags["vegan"] is False
        assert flags["sans-lactose"] is False

    def test_empty_filter_string(self):
        flags = _parse_diet_flags("")
        assert all(v is False for v in flags.values())

    def test_none_filter_string(self):
        flags = _parse_diet_flags(None)
        assert all(v is False for v in flags.values())

    def test_handles_casing_variants(self):
        # Observed in prod: "Pork_Free", "Gluten_Free Pork_Free", "PORK_free", "Vegetarian"
        flags = _parse_diet_flags("Gluten_Free Pork_Free")
        assert flags["sans-gluten"] is True
        assert flags["sans-porc"] is True

    def test_handles_extra_whitespace(self):
        # Observed: "gluten_free  pork_free", "pork_free   vegetarian"
        flags = _parse_diet_flags("gluten_free   pork_free   vegetarian")
        assert flags["sans-gluten"] is True
        assert flags["sans-porc"] is True
        assert flags["vegetarien"] is True

    def test_handles_known_typos(self):
        # Observed: "lactos_free vegetarien", "factose_free", "pork_free factose_free"
        flags = _parse_diet_flags("lactos_free factose_free")
        assert flags["sans-lactose"] is True

    def test_french_variants(self):
        flags = _parse_diet_flags("vegetarien")
        assert flags["vegetarien"] is True
        flags = _parse_diet_flags("végétarien")
        assert flags["vegetarien"] is True

    def test_ignores_non_diet_tokens(self):
        # Observed: "first_pos", "1stpos", "paleo", "paleolithic", "cold_meal",
        # "boisson", "dessert", "protein_rich", "5 - 7"
        flags = _parse_diet_flags("first_pos paleo cold_meal protein_rich 1stpos boisson")
        assert all(v is False for v in flags.values())

    def test_mixes_known_and_unknown(self):
        # "vegetarian gluten_free lactose_free pork_free paleo" → ignore paleo only
        flags = _parse_diet_flags("vegetarian gluten_free lactose_free pork_free paleo")
        assert flags["vegetarien"] is True
        assert flags["sans-gluten"] is True
        assert flags["sans-lactose"] is True
        assert flags["sans-porc"] is True
        assert flags["meat"] is False  # paleo ignored
        assert flags["fitness"] is False

    def test_canonical_slug_order(self):
        # Template iterates dict items in order; assert badges always
        # appear in vegetarien → vegan → … → fitness order.
        flags = _parse_diet_flags("meat fitness vegetarian")
        assert list(flags.keys()) == [
            "vegetarien", "vegan", "sans-gluten", "sans-lactose",
            "sans-porc", "meat", "fish", "fitness",
        ]


class TestBuildTags:
    """Tags are sorted (stable payload) and slug-only."""

    def _diets(self, **overrides) -> dict[str, bool]:
        base = {
            "vegetarien": False, "vegan": False, "sans-gluten": False,
            "sans-lactose": False, "sans-porc": False, "meat": False,
            "fish": False, "fitness": False,
        }
        base.update(overrides)
        return base

    def test_diets_only(self):
        tags = _build_tags(
            diets=self._diets(meat=True, **{"sans-gluten": True}),
            is_active_today=False,
            category=None,
            nutri_score=None,
        )
        assert tags == ["meat", "sans-gluten"]

    def test_current_menu_when_active(self):
        tags = _build_tags(
            diets=self._diets(),
            is_active_today=True,
            category=None,
            nutri_score=None,
        )
        assert "current-menu" in tags

    def test_no_current_menu_when_inactive(self):
        tags = _build_tags(
            diets=self._diets(),
            is_active_today=False,
            category=None,
            nutri_score=None,
        )
        assert "current-menu" not in tags

    def test_category_kebab_case(self):
        # MAIN_DISH → main-dish, NON_FOOD → non-food
        tags = _build_tags(
            diets=self._diets(), is_active_today=False,
            category="MAIN_DISH", nutri_score=None,
        )
        assert "main-dish" in tags
        tags = _build_tags(
            diets=self._diets(), is_active_today=False,
            category="NON_FOOD", nutri_score=None,
        )
        assert "non-food" in tags

    def test_category_none_or_empty_skipped(self):
        for cat in (None, ""):
            tags = _build_tags(
                diets=self._diets(), is_active_today=False,
                category=cat, nutri_score=None,
            )
            assert not any(t.startswith("main-") or t.startswith("non-") for t in tags)

    def test_nutri_score_lowercased(self):
        for score, expected in [("A", "nutri-a"), ("B", "nutri-b"), ("E", "nutri-e")]:
            tags = _build_tags(
                diets=self._diets(), is_active_today=False,
                category=None, nutri_score=score,
            )
            assert expected in tags

    def test_full_meal_tag_set_is_sorted(self):
        tags = _build_tags(
            diets=self._diets(meat=True, **{"sans-gluten": True}),
            is_active_today=True,
            category="MAIN_DISH",
            nutri_score="A",
        )
        assert tags == [
            "current-menu", "main-dish", "meat", "nutri-a", "sans-gluten",
        ]

    def test_empty_diets_dict(self):
        tags = _build_tags(
            diets={}, is_active_today=False, category=None, nutri_score=None,
        )
        assert tags == []


class TestPlanLabel:
    def test_known_aliases_collapse_to_canonical(self):
        assert _plan_label("standard") == "Standard"
        assert _plan_label("Standard") == "Standard"
        assert _plan_label("normale") == "Standard"
        assert _plan_label("NORMALE") == "Standard"
        assert _plan_label("large") == "Large"
        assert _plan_label("LARGE") == "Large"

    def test_unknown_plan_name_title_cased(self):
        assert _plan_label("xl_plan") == "Xl_Plan"
        assert _plan_label("kids") == "Kids"

    def test_empty_or_none(self):
        assert _plan_label(None) == ""
        assert _plan_label("") == ""


class TestBuildVariants:
    """For MAIN_DISH/BREAKFAST: one variant per plan, price = plan.additional +
    meal.unit + meal.extra. Other categories: single variant from unit + extra."""

    @staticmethod
    def _plans() -> list[dict]:
        return [
            {"plan_id": 1, "plan_label": "Standard", "additional_meal_price": 5},
            {"plan_id": 2, "plan_label": "Large",    "additional_meal_price": 7},
        ]

    def test_main_dish_two_variants(self):
        variants = _build_variants(
            meal_id=42, category="MAIN_DISH",
            unit_price=4, extra_price=0.5, plans=self._plans(),
        )
        assert variants == [
            {"price": "9.50",  "option1": "Standard", "sku": "fresheo-42-1"},
            {"price": "11.50", "option1": "Large",    "sku": "fresheo-42-2"},
        ]

    def test_breakfast_two_variants(self):
        variants = _build_variants(
            meal_id=7, category="BREAKFAST",
            unit_price=0, extra_price=0, plans=self._plans(),
        )
        assert [v["option1"] for v in variants] == ["Standard", "Large"]
        assert variants[0]["price"] == "5.00"
        assert variants[1]["price"] == "7.00"

    def test_drinks_single_variant_no_option(self):
        variants = _build_variants(
            meal_id=12, category="DRINKS",
            unit_price=2.5, extra_price=0, plans=self._plans(),
        )
        assert variants == [
            {"price": "2.50", "sku": "fresheo-12"},
        ]

    def test_snack_single_variant(self):
        variants = _build_variants(
            meal_id=99, category="SNACK",
            unit_price=1.99, extra_price=0.01, plans=self._plans(),
        )
        assert variants == [
            {"price": "2.00", "sku": "fresheo-99"},
        ]

    def test_non_food_single_variant(self):
        variants = _build_variants(
            meal_id=100, category="NON_FOOD",
            unit_price=10, extra_price=0, plans=self._plans(),
        )
        assert variants == [
            {"price": "10.00", "sku": "fresheo-100"},
        ]

    def test_variants_carry_no_rest_inventory_field(self):
        # The REST `inventory_management` field was removed in API 2024-04, so
        # the build must NOT emit it — tracking is disabled out-of-band via a
        # GraphQL productVariantsBulkUpdate. Emitting it would be a silent no-op
        # and mislead readers into thinking REST controls tracking.
        main_dish = _build_variants(
            meal_id=42, category="MAIN_DISH",
            unit_price=4, extra_price=0.5, plans=self._plans(),
        )
        drinks = _build_variants(
            meal_id=12, category="DRINKS",
            unit_price=2.5, extra_price=0, plans=self._plans(),
        )
        for v in main_dish + drinks:
            assert "inventory_management" not in v

    def test_none_prices_treated_as_zero(self):
        variants = _build_variants(
            meal_id=1, category="MAIN_DISH",
            unit_price=None, extra_price=None, plans=self._plans(),
        )
        assert variants[0]["price"] == "5.00"
        assert variants[1]["price"] == "7.00"

    def test_sku_uses_plan_id(self):
        # SKU keys on plan_id so renaming a plan label doesn't shift SKUs.
        plans = [
            {"plan_id": 5, "plan_label": "XL Plan", "additional_meal_price": 10},
        ]
        variants = _build_variants(
            meal_id=42, category="MAIN_DISH",
            unit_price=0, extra_price=0, plans=plans,
        )
        assert variants == [
            {"price": "10.00", "option1": "XL Plan", "sku": "fresheo-42-5"},
        ]

    def test_main_dish_falls_back_to_single_when_no_plans(self):
        # Defensive: if menu_plan is empty (misconfigured DB), don't crash —
        # emit a single variant from raw unit/extra prices.
        variants = _build_variants(
            meal_id=7, category="MAIN_DISH",
            unit_price=4, extra_price=0.5, plans=[],
        )
        assert variants == [
            {"price": "4.50", "sku": "fresheo-7"},
        ]


class TestIsActiveToday:
    """Mirrors Django Meal.active_at(): active_on <= today <= inactive_on
    (both inclusive). NULL on either side → NOT active.
    """

    def test_today_inside_window(self):
        from datetime import date, timedelta
        today = date.today()
        assert _is_active_today(today - timedelta(days=1), today + timedelta(days=1)) is True

    def test_today_equals_active_on(self):
        # __lte → inclusive lower bound
        from datetime import date, timedelta
        today = date.today()
        assert _is_active_today(today, today + timedelta(days=5)) is True

    def test_today_equals_inactive_on(self):
        # __gte → inclusive upper bound
        from datetime import date, timedelta
        today = date.today()
        assert _is_active_today(today - timedelta(days=5), today) is True

    def test_not_yet_started(self):
        from datetime import date, timedelta
        today = date.today()
        assert _is_active_today(today + timedelta(days=1), today + timedelta(days=5)) is False

    def test_already_ended(self):
        from datetime import date, timedelta
        today = date.today()
        assert _is_active_today(today - timedelta(days=5), today - timedelta(days=1)) is False

    def test_null_active_on_is_not_active(self):
        from datetime import date, timedelta
        assert _is_active_today(None, date.today() + timedelta(days=5)) is False

    def test_null_inactive_on_is_not_active(self):
        from datetime import date, timedelta
        assert _is_active_today(date.today() - timedelta(days=5), None) is False

    def test_both_null_is_not_active(self):
        assert _is_active_today(None, None) is False


class TestCategoryLabel:
    def test_known_categories(self):
        assert category_label_fr("MAIN_DISH") == "Repas"
        assert category_label_fr("BREAKFAST") == "Petit déjeuner"
        assert category_label_fr("DESSERT") == "Dessert"
        assert category_label_fr("DRINKS") == "Boissons"
        assert category_label_fr("VACUUM") == "Fresheo deals"
        assert category_label_fr("SNACK") == "Snacks"

    def test_unknown_category(self):
        assert category_label_fr("BOGUS") == ""

    def test_none_or_empty(self):
        assert category_label_fr(None) == ""
        assert category_label_fr("") == ""


class TestCategoryTaxonomyGid:
    """Maps Meal.category → Shopify taxonomy GID; this drives Belgian VAT.
    Food categories → 6% food nodes; unmapped → None (uncategorized = 21%)."""

    def test_food_categories_map_to_prepared_food_nodes(self):
        assert category_taxonomy_gid("MAIN_DISH") == (
            "gid://shopify/TaxonomyCategory/fb-2-15-2"
        )
        assert category_taxonomy_gid("BREAKFAST") == (
            "gid://shopify/TaxonomyCategory/fb-2-15-2"
        )
        assert category_taxonomy_gid("DESSERT") == (
            "gid://shopify/TaxonomyCategory/fb-2-15-3"
        )
        assert category_taxonomy_gid("SNACK") == (
            "gid://shopify/TaxonomyCategory/fb-2-17"
        )

    def test_drinks_maps_to_beverages(self):
        assert category_taxonomy_gid("DRINKS") == (
            "gid://shopify/TaxonomyCategory/fb-1"
        )

    def test_unmapped_categories_return_none(self):
        # VACUUM / NON_FOOD are deliberately uncategorized → 21% standard rate.
        assert category_taxonomy_gid("VACUUM") is None
        assert category_taxonomy_gid("NON_FOOD") is None

    def test_unknown_or_empty_returns_none(self):
        assert category_taxonomy_gid("BOGUS") is None
        assert category_taxonomy_gid(None) is None
        assert category_taxonomy_gid("") is None


class TestFormatPrice:
    def test_none_returns_none(self):
        assert _format_price(None) is None

    def test_decimal_string_formatted_to_two_places(self):
        from decimal import Decimal
        assert _format_price(Decimal("9.5")) == "9.50"
        assert _format_price(Decimal("9.999")) == "10.00"

    def test_float_formatted_to_two_places(self):
        assert _format_price(7) == "7.00"


class TestResolveImageUrls:
    def test_prefers_meal_image_when_set(self):
        urls = _resolve_image_urls(
            {"meal_image": "https://cdn.x/a.jpg", "picture_path": "pictures/meals/b.jpg"},
            media_url="https://media.example.com/",
        )
        assert urls == ["https://cdn.x/a.jpg"]

    def test_falls_back_to_picture_with_media_url(self):
        urls = _resolve_image_urls(
            {"meal_image": "", "picture_path": "pictures/meals/b.jpg"},
            media_url="https://media.example.com/",
        )
        assert urls == ["https://media.example.com/pictures/meals/b.jpg"]

    def test_returns_empty_when_no_media_url(self):
        urls = _resolve_image_urls(
            {"meal_image": "", "picture_path": "pictures/meals/b.jpg"}, media_url=""
        )
        assert urls == []

    def test_encodes_spaces_in_meal_image_url(self):
        urls = _resolve_image_urls(
            {"meal_image": "https://s3.example.com/meals/Riz cantonais.jpeg",
             "picture_path": ""},
            media_url="",
        )
        assert urls == ["https://s3.example.com/meals/Riz%20cantonais.jpeg"]


class TestPercentEncodeUrl:
    def test_encodes_spaces(self):
        assert _percent_encode_url("https://x.com/a b/c.jpg") == "https://x.com/a%20b/c.jpg"

    def test_preserves_path_separators(self):
        assert _percent_encode_url("https://x.com/a/b/c.jpg") == "https://x.com/a/b/c.jpg"

    def test_idempotent_on_already_encoded(self):
        already = "https://x.com/a%20b/c.jpg"
        assert _percent_encode_url(already) == already

    def test_preserves_query_string(self):
        url = "https://x.com/a b.jpg?v=1"
        assert _percent_encode_url(url) == "https://x.com/a%20b.jpg?v=1"


# ── Renderer ────────────────────────────────────────────────────────────────

class TestRenderer:
    def test_renders_nutri_score_active_class(self, renderer, sample_meal):
        sample_meal["nutri_score"] = "C"
        html = renderer.render(sample_meal)
        assert 'class="fresheo-nutri-badge nutri-c active"' in html
        assert 'class="fresheo-nutri-badge nutri-a"' in html  # not active

    def test_renders_rating(self, renderer, sample_meal):
        html = renderer.render(sample_meal)
        assert "4.7" in html
        assert "(42 avis)" in html

    def test_renders_macros(self, renderer, sample_meal):
        html = renderer.render(sample_meal)
        assert "254" in html
        assert "32.54" in html
        assert "par portion de 450g" in html

    def test_renders_only_active_diet_badges(self, renderer, sample_meal):
        html = renderer.render(sample_meal)
        assert "🥩 Viande" in html
        assert "🌾 Sans gluten" in html
        assert "Végétarien" not in html

    def test_empty_allergens_shows_fallback(self, renderer, sample_meal):
        html = renderer.render(sample_meal)
        assert "aucun allergène majeur" in html

    def test_allergens_present_overrides_fallback(self, renderer, sample_meal):
        sample_meal["allergens"] = "Gluten, œufs"
        html = renderer.render(sample_meal)
        # Jinja autoescapes œ — verify the original word isn't in fallback context
        assert "aucun allergène majeur" not in html
        assert "ufs" in html  # œ gets escaped to &#339;, "ufs" survives


# ── _escape_for_query ───────────────────────────────────────────────────────

class TestSlugify:
    def test_strips_accents(self):
        assert _slugify("Poulet & légumes") == "poulet-legumes"

    def test_strips_trailing_whitespace(self):
        # This is the duplicate-causing case from production: the Django title
        # has a trailing space; without stripping the handle would differ run-to-run.
        assert _slugify("Poisson pané ") == _slugify("Poisson pané")

    def test_curly_and_straight_apostrophes_collapse(self):
        # U+2019 vs U+0027 — both should produce the same handle.
        assert _slugify("Poisson à l’avoine") == _slugify("Poisson à l'avoine")

    def test_collapses_punctuation_and_spaces(self):
        assert _slugify("Riz cantonais, scampis & co.") == "riz-cantonais-scampis-co"

    def test_idempotent(self):
        title = "Farfalles aux herbes"
        once = _slugify(title)
        twice = _slugify(once)  # slug fed back as input
        assert _slugify(title) == once  # same input → same output


class TestEscapeForQuery:
    def test_escapes_quotes(self):
        assert _escape_for_query('A "B" C') == 'A \\"B\\" C'

    def test_escapes_backslashes(self):
        assert _escape_for_query("A\\B") == "A\\\\B"

    def test_passthrough_plain(self):
        assert _escape_for_query("Poulet & légumes") == "Poulet & légumes"


# ── Diff helpers ────────────────────────────────────────────────────────────


class TestParseImageSources:
    def test_empty_value(self):
        assert _parse_image_sources("") == []
        assert _parse_image_sources(None) == []  # type: ignore[arg-type]

    def test_json_array(self):
        assert _parse_image_sources('["https://a.com/x.jpg", "https://b.com/y.jpg"]') == [
            "https://a.com/x.jpg", "https://b.com/y.jpg",
        ]

    def test_csv_fallback(self):
        # Tolerance for legacy single_line_text_field shape.
        assert _parse_image_sources("https://a.com/x.jpg, https://b.com/y.jpg") == [
            "https://a.com/x.jpg", "https://b.com/y.jpg",
        ]

    def test_roundtrip_via_serialize(self):
        urls = ["https://a.com/x.jpg", "https://b.com/y%20space.png"]
        assert _parse_image_sources(_serialize_image_sources(urls)) == urls


class TestDiffProduct:
    def _payload(self) -> dict:
        return {
            "title": "Poulet",
            "body_html": "<p>x</p>",
            "tags": "a, b, c",
            "product_type": "Repas",
            "variants": [
                {"sku": "fresheo-42-1", "price": "9.50", "option1": "Standard"},
                {"sku": "fresheo-42-2", "price": "11.50", "option1": "Large"},
            ],
            "options": [{"name": "Size", "values": ["Standard", "Large"]}],
        }

    def _matching_existing(self) -> dict:
        p = self._payload()
        return {
            "title": p["title"],
            "body_html": p["body_html"],
            "tags": ["a", "b", "c"],
            "product_type": p["product_type"],
            "variants": [
                {"sku": v["sku"], "price": v["price"], "option1": v["option1"],
                 # Untracked existing variant (steady state after the GraphQL
                 # disable). `inventory_management` lives on the existing shape
                 # but is intentionally ignored by `_variants_differ`.
                 "inventory_management": None}
                for v in p["variants"]
            ],
            "options": p["options"],
        }

    def test_no_diff_when_everything_matches(self):
        assert _diff_product(self._matching_existing(), self._payload()) == {}

    def test_title_change(self):
        existing = self._matching_existing()
        existing["title"] = "Old name"
        diff = _diff_product(existing, self._payload())
        assert diff == {"title": "Poulet"}

    def test_body_html_change(self):
        existing = self._matching_existing()
        existing["body_html"] = "<p>old</p>"
        diff = _diff_product(existing, self._payload())
        assert diff == {"body_html": "<p>x</p>"}

    def test_product_type_change(self):
        existing = self._matching_existing()
        existing["product_type"] = "Other"
        diff = _diff_product(existing, self._payload())
        assert diff == {"product_type": "Repas"}

    def test_tags_set_equality_ignores_order_and_whitespace(self):
        existing = self._matching_existing()
        existing["tags"] = ["c", "a", "b"]
        assert _diff_product(existing, self._payload()) == {}

    def test_tags_change_emits_new_csv(self):
        existing = self._matching_existing()
        existing["tags"] = ["a", "b"]  # missing 'c'
        diff = _diff_product(existing, self._payload())
        assert diff == {"tags": "a, b, c"}

    def test_variant_price_change_emits_full_variant_list(self):
        existing = self._matching_existing()
        existing["variants"][0]["price"] = "8.50"
        diff = _diff_product(existing, self._payload())
        assert "variants" in diff
        assert len(diff["variants"]) == 2

    def test_added_variant_triggers_diff(self):
        existing = self._matching_existing()
        existing["variants"] = existing["variants"][:1]  # drop Large
        diff = _diff_product(existing, self._payload())
        assert "variants" in diff

    def test_options_order_insensitive(self):
        existing = self._matching_existing()
        existing["options"] = [{"name": "Size", "values": ["Large", "Standard"]}]
        assert _diff_product(existing, self._payload()) == {}

    def test_options_name_change(self):
        existing = self._matching_existing()
        existing["options"] = [{"name": "Taille", "values": ["Standard", "Large"]}]
        diff = _diff_product(existing, self._payload())
        assert "options" in diff

    def test_single_variant_payload_never_emits_empty_options(self):
        # Single-variant categories (DESSERT/DRINKS/SNACK) ship payload with no
        # `options` key. Shopify always carries an implicit Title option on
        # existing products and rejects PUT options=[] with 422
        # 'could not update options to []' — diff must not emit it.
        existing = self._matching_existing()
        existing["options"] = [{"name": "Title", "values": ["Default Title"]}]
        payload = self._payload()
        del payload["options"]
        diff = _diff_product(existing, payload)
        assert "options" not in diff


class TestVariantsDiffer:
    def test_equal_on_price_and_option(self):
        a = [{"sku": "x", "price": "9.50", "option1": "Standard"}]
        b = [{"sku": "x", "price": "9.50", "option1": "Standard"}]
        assert _variants_differ(a, b) is False

    def test_tracking_drift_alone_does_not_differ(self):
        # The whole point of the 2024-04 decoupling: a tracked-vs-untracked
        # mismatch must NOT force a variant replacement (it would churn IDs and
        # break image links). Tracking is reconciled via GraphQL instead.
        existing = [{"sku": "x", "price": "9.50", "option1": "Standard",
                     "inventory_management": "shopify"}]
        new = [{"sku": "x", "price": "9.50", "option1": "Standard"}]
        assert _variants_differ(existing, new) is False

    def test_price_change_differs(self):
        existing = [{"sku": "x", "price": "8.00", "option1": "Standard"}]
        new = [{"sku": "x", "price": "9.50", "option1": "Standard"}]
        assert _variants_differ(existing, new) is True


class TestNeedsTrackingDisable:
    def test_tracked_variant_needs_disable(self):
        assert _needs_tracking_disable(
            [{"sku": "x", "inventory_management": "shopify"}]
        ) is True

    def test_all_untracked_needs_nothing(self):
        assert _needs_tracking_disable(
            [{"sku": "x", "inventory_management": None},
             {"sku": "y", "inventory_management": None}]
        ) is False

    def test_any_tracked_variant_triggers(self):
        assert _needs_tracking_disable(
            [{"sku": "x", "inventory_management": None},
             {"sku": "y", "inventory_management": "shopify"}]
        ) is True

    def test_empty_list(self):
        assert _needs_tracking_disable([]) is False


class TestDiffImages:
    def test_empty_sets_match(self):
        assert _diff_images([], []) is False

    def test_same_urls_in_any_order(self):
        assert _diff_images(["a", "b"], ["b", "a"]) is False

    def test_added_url(self):
        assert _diff_images(["a"], ["a", "b"]) is True

    def test_removed_url(self):
        assert _diff_images(["a", "b"], ["a"]) is True

    def test_changed_url(self):
        assert _diff_images(["a"], ["b"]) is True


class TestDiffPublications:
    pub_a = "gid://shopify/Publication/1"
    pub_b = "gid://shopify/Publication/2"

    def test_state_already_matches_target_returns_none(self):
        existing = {self.pub_a: True, self.pub_b: True}
        assert _diff_publications(existing, [self.pub_a, self.pub_b], True) is None

    def test_no_publications_means_no_diff(self):
        assert _diff_publications({}, [], True) is None

    def test_one_publication_needs_publish(self):
        existing = {self.pub_a: True, self.pub_b: False}
        diff = _diff_publications(existing, [self.pub_a, self.pub_b], True)
        assert diff == {"mutation": "publish", "pub_gids": [self.pub_b]}

    def test_all_publications_need_unpublish(self):
        existing = {self.pub_a: True, self.pub_b: True}
        diff = _diff_publications(existing, [self.pub_a, self.pub_b], False)
        assert diff == {"mutation": "unpublish", "pub_gids": [self.pub_a, self.pub_b]}

    def test_missing_existing_treated_as_false(self):
        # Pub never seen on this product → defaults to unpublished. Target
        # is published → needs publish.
        diff = _diff_publications({}, [self.pub_a], True)
        assert diff == {"mutation": "publish", "pub_gids": [self.pub_a]}


class TestNeedsSubscriptionAssociation:
    def test_target_missing_returns_false(self):
        assert _needs_subscription_association(["gid://x"], None) is False
        assert _needs_subscription_association([], "") is False

    def test_already_associated(self):
        gid = "gid://shopify/SellingPlanGroup/500"
        assert _needs_subscription_association([gid], gid) is False

    def test_not_yet_associated(self):
        gid = "gid://shopify/SellingPlanGroup/500"
        assert _needs_subscription_association([], gid) is True
        assert _needs_subscription_association(["gid://other"], gid) is True


# ── MealsResource load (upsert) ─────────────────────────────────────────────

class TestMealsLoad:
    async def test_creates_when_no_existing_product(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        # Handle lookup returns empty; GraphQL fallback also empty → create
        mock_dest_client.get = AsyncMock(return_value={"products": []})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql())
        mock_dest_client.post = AsyncMock(return_value={"product": {"id": 9001}})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))

        await resource.load()

        # Handle GET happened with our deterministic slug
        get_call = mock_dest_client.get.await_args
        assert get_call.args[0] == "products.json"
        assert get_call.kwargs["params"]["handle"] == _slugify(sample_meal["name"])
        # POST to create
        post_calls = mock_dest_client.post.await_args_list
        assert len(post_calls) == 1
        assert post_calls[0].args[0] == "products.json"
        sent = post_calls[0].args[1]["product"]
        assert sent["title"] == sample_meal["name"]
        assert sent["handle"] == _slugify(sample_meal["name"])
        assert sent["published"] is True  # is_active_today=True
        assert "fresheo-product" in sent["body_html"]
        mock_dest_client.put.assert_not_awaited()
        mock_dest_client.delete.assert_not_awaited()

    async def test_handle_lookup_avoids_duplicate_on_rerun(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        # Simulate a meal that already exists on Shopify with our deterministic handle.
        # Handle GET finds it; getProductById fills in the diffable shape; no
        # findByTitle fallback should be needed.
        existing_handle = _slugify(sample_meal["name"])
        mock_dest_client.get = AsyncMock(return_value={
            "products": [{
                "id": 9001,
                "admin_graphql_api_id": "gid://shopify/Product/9001",
                "title": sample_meal["name"],
                "handle": existing_handle,
            }]
        })
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql(
            get_product_response=_existing_node(),  # empty fields → diff fires
        ))
        mock_dest_client.put = AsyncMock(return_value={})
        mock_dest_client.delete = AsyncMock(return_value=None)
        mock_dest_client.post = AsyncMock(return_value={"image": {"id": 999}})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        # Handle hit → no findByTitle fallback (but getProductById IS called).
        gql_queries = [c.args[0] for c in mock_dest_client.graphql.await_args_list]
        assert not any("findByTitle" in q for q in gql_queries)
        assert any("getProductById" in q for q in gql_queries)
        # Update path, not create
        post_paths = [c.args[0] for c in mock_dest_client.post.await_args_list]
        assert "products.json" not in post_paths  # no create
        # Sparse PUT — exactly one PUT to products/9001.json.
        put_paths = [c.args[0] for c in mock_dest_client.put.await_args_list]
        assert put_paths == ["products/9001.json"]

    async def test_title_with_trailing_space_is_idempotent(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        # Reproduces the production duplicate bug: title has trailing space.
        # Both runs must resolve to the same handle.
        sample_meal["name"] = "Poisson pané "  # trailing space
        existing_handle = _slugify(sample_meal["name"])
        assert existing_handle == "poisson-pane"  # whitespace stripped

        mock_dest_client.get = AsyncMock(return_value={
            "products": [{
                "id": 9001, "admin_graphql_api_id": "gid://shopify/Product/9001",
                "title": "Poisson pané", "handle": existing_handle,
            }]
        })
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql(
            get_product_response=_existing_node(),
        ))
        mock_dest_client.put = AsyncMock(return_value={})
        mock_dest_client.delete = AsyncMock(return_value=None)
        mock_dest_client.post = AsyncMock(return_value={})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        # No create — existing product found via handle
        post_paths = [c.args[0] for c in mock_dest_client.post.await_args_list]
        assert "products.json" not in post_paths

    async def test_updates_when_product_exists(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        # Handle lookup misses; GraphQL fallback finds the product (simulates a
        # legacy product whose Shopify-auto-generated handle differs from our slug).
        mock_dest_client.get = AsyncMock(return_value={"products": []})
        title_hit = {"products": {"edges": [{
            "node": _existing_node(image_ids=["111", "112"]),
        }]}}
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql(title_hit))
        mock_dest_client.put = AsyncMock(return_value={"product": {"id": 9001}})
        mock_dest_client.delete = AsyncMock(return_value=None)
        mock_dest_client.post = AsyncMock(return_value={"image": {"id": 999}})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))

        await resource.load()

        # Sparse PUT — exactly one PUT carrying the changed fields.
        put_calls = mock_dest_client.put.await_args_list
        assert len(put_calls) == 1
        assert put_calls[0].args[0] == "products/9001.json"
        sent_product = put_calls[0].args[1]["product"]
        assert "fresheo-product" in sent_product["body_html"]
        assert sent_product["options"] == [
            {"name": "Size", "values": ["Standard", "Large"]}
        ]
        assert [v["option1"] for v in sent_product["variants"]] == ["Standard", "Large"]
        assert [v["price"] for v in sent_product["variants"]] == ["9.50", "11.50"]

        # Image refresh fires (image_sources metafield absent on existing).
        # DELETE called once per existing image (2)
        assert mock_dest_client.delete.await_count == 2
        # POST to add the new image (1 in sample_meal) + metafield write.
        post_paths = [c.args[0] for c in mock_dest_client.post.await_args_list]
        assert "products/9001/images.json" in post_paths
        assert "products/9001/metafields.json" in post_paths

    async def test_dry_run_skips_writes(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        mock_dest_client.get = AsyncMock(return_value={"products": []})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql())

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer, dry_run=True
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))

        await resource.load()

        mock_dest_client.put.assert_not_awaited()
        mock_dest_client.post.assert_not_awaited()
        mock_dest_client.delete.assert_not_awaited()

    async def test_create_payload_includes_product_type(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        mock_dest_client.get = AsyncMock(return_value={"products": []})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql())
        mock_dest_client.post = AsyncMock(return_value={"product": {"id": 9001}})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        sent = mock_dest_client.post.await_args.args[1]["product"]
        assert sent["product_type"] == "Repas"

    async def test_update_payload_includes_product_type(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        # Handle lookup hit — goes down update path. Existing has empty
        # product_type → diff fires → sparse PUT carries the new value.
        mock_dest_client.get = AsyncMock(return_value={
            "products": [{
                "id": 9001, "admin_graphql_api_id": "gid://shopify/Product/9001",
                "title": sample_meal["name"], "handle": _slugify(sample_meal["name"]),
            }]
        })
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql(
            get_product_response=_existing_node(),
        ))
        mock_dest_client.put = AsyncMock(return_value={})
        mock_dest_client.delete = AsyncMock(return_value=None)
        mock_dest_client.post = AsyncMock(return_value={})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        body_put = mock_dest_client.put.await_args_list[0]
        assert body_put.args[0] == "products/9001.json"
        assert body_put.args[1]["product"]["product_type"] == "Repas"

    async def test_create_payload_includes_tags(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        mock_dest_client.get = AsyncMock(return_value={"products": []})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql())
        mock_dest_client.post = AsyncMock(return_value={"product": {"id": 9001}})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        sent = mock_dest_client.post.await_args.args[1]["product"]
        assert sent["tags"] == "current-menu, main-dish, meat, nutri-a, sans-gluten"

    async def test_update_payload_includes_tags(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        # Handle lookup hit → update path. Existing tags empty → tags in diff.
        mock_dest_client.get = AsyncMock(return_value={
            "products": [{
                "id": 9001, "admin_graphql_api_id": "gid://shopify/Product/9001",
                "title": sample_meal["name"], "handle": _slugify(sample_meal["name"]),
            }]
        })
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql(
            get_product_response=_existing_node(),
        ))
        mock_dest_client.put = AsyncMock(return_value={})
        mock_dest_client.delete = AsyncMock(return_value=None)
        mock_dest_client.post = AsyncMock(return_value={})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        body_put = mock_dest_client.put.await_args_list[0]
        assert body_put.args[0] == "products/9001.json"
        assert body_put.args[1]["product"]["tags"] == (
            "current-menu, main-dish, meat, nutri-a, sans-gluten"
        )

    async def test_inactive_meal_drops_current_menu_tag(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        # Same fixture but expired window: current-menu should be absent.
        sample_meal["is_active_today"] = False
        sample_meal["tags"] = ["main-dish", "meat", "nutri-a", "sans-gluten"]

        mock_dest_client.get = AsyncMock(return_value={
            "products": [{
                "id": 9001, "admin_graphql_api_id": "gid://shopify/Product/9001",
                "title": sample_meal["name"], "handle": _slugify(sample_meal["name"]),
            }]
        })
        # Existing carries the old tag set including 'current-menu' → diff fires.
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql(
            get_product_response=_existing_node(
                tags=["current-menu", "main-dish", "meat", "nutri-a", "sans-gluten"],
            ),
        ))
        mock_dest_client.put = AsyncMock(return_value={})
        mock_dest_client.delete = AsyncMock(return_value=None)
        mock_dest_client.post = AsyncMock(return_value={})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        sent_tags = mock_dest_client.put.await_args_list[0].args[1]["product"]["tags"]
        assert "current-menu" not in sent_tags
        assert sent_tags == "main-dish, meat, nutri-a, sans-gluten"

    async def test_reconcile_strips_current_menu_from_absent_meals(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        # One active meal in the extract → created as product 9001 (kept). The
        # store also carries 'current-menu' on product 7777, a meal that has
        # dropped out of the menu and is absent from the extract entirely. The
        # post-load reconciliation sweep must strip the tag from 7777 only.
        mock_dest_client.get = AsyncMock(return_value={"products": []})
        mock_dest_client.post = AsyncMock(
            return_value={"product": {"id": 9001, "variants": [], "images": []}}
        )
        mock_dest_client.put = AsyncMock(return_value={})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql(
            products_by_tag_edges=[
                {"id": "gid://shopify/Product/9001", "legacyResourceId": "9001",
                 "title": sample_meal["name"], "tags": ["current-menu", "main-dish"]},
                {"id": "gid://shopify/Product/7777", "legacyResourceId": "7777",
                 "title": "Vieux plat", "tags": ["current-menu", "dessert"]},
            ],
        ))

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        # Reconciliation PUTs target the product root (not the /images/ subpath)
        # and carry a tags field.
        reconcile_puts = [
            c for c in mock_dest_client.put.await_args_list
            if "/images/" not in c.args[0] and "tags" in c.args[1].get("product", {})
        ]
        assert len(reconcile_puts) == 1
        assert reconcile_puts[0].args[0] == "products/7777.json"
        # current-menu stripped; the meal's other tags are preserved.
        assert reconcile_puts[0].args[1]["product"]["tags"] == "dessert"

    async def test_reconcile_dry_run_makes_no_writes(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        mock_dest_client.get = AsyncMock(return_value={"products": []})
        mock_dest_client.put = AsyncMock(return_value={})
        mock_dest_client.post = AsyncMock(return_value={})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql(
            products_by_tag_edges=[
                {"id": "gid://shopify/Product/7777", "legacyResourceId": "7777",
                 "title": "Vieux plat", "tags": ["current-menu", "dessert"]},
            ],
        ))

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer,
            dry_run=True,
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        mock_dest_client.put.assert_not_awaited()

    async def test_create_payload_includes_variants_and_options(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        mock_dest_client.get = AsyncMock(return_value={"products": []})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql())
        mock_dest_client.post = AsyncMock(return_value={"product": {"id": 9001}})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        sent_payload = mock_dest_client.post.await_args.args[1]["product"]
        assert sent_payload["options"] == [
            {"name": "Size", "values": ["Standard", "Large"]}
        ]
        assert sent_payload["variants"] == [
            {"price": "9.50",  "option1": "Standard", "sku": "fresheo-42-1"},
            {"price": "11.50", "option1": "Large",    "sku": "fresheo-42-2"},
        ]

    async def test_single_variant_category_omits_options(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        # DRINKS / SNACK / NON_FOOD don't scale with menu size — single variant,
        # no Size option set on the product.
        sample_meal["category"] = "DRINKS"
        sample_meal["category_label"] = "Boissons"
        sample_meal["category_gid"] = "gid://shopify/TaxonomyCategory/fb-1"
        sample_meal["options"] = []
        sample_meal["variants"] = [
            {"price": "2.50", "sku": "fresheo-42"},
        ]

        mock_dest_client.get = AsyncMock(return_value={"products": []})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql())
        mock_dest_client.post = AsyncMock(return_value={"product": {"id": 9001}})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        sent = mock_dest_client.post.await_args.args[1]["product"]
        assert "options" not in sent
        assert sent["variants"] == [
            {"price": "2.50", "sku": "fresheo-42"},
        ]

    async def test_update_replaces_variants_via_product_put(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        # Pre-existing product has one default variant (option1=null, sku=foo).
        # The diff fires for variants and options; sparse PUT carries the full
        # new variants list and options in a single product PUT body.
        mock_dest_client.get = AsyncMock(return_value={
            "products": [{
                "id": 9001, "admin_graphql_api_id": "gid://shopify/Product/9001",
                "title": sample_meal["name"], "handle": _slugify(sample_meal["name"]),
            }]
        })
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql(
            get_product_response=_existing_node(
                variants_shape=[{"sku": "legacy", "price": "5.00",
                                 "option1": None, "inventory_management": None}],
                options=[],
            ),
        ))
        mock_dest_client.put = AsyncMock(return_value={})
        mock_dest_client.delete = AsyncMock(return_value=None)
        mock_dest_client.post = AsyncMock(return_value={})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        put_calls = mock_dest_client.put.await_args_list
        assert len(put_calls) == 1
        sent = put_calls[0].args[1]["product"]
        assert sent["options"] == [
            {"name": "Size", "values": ["Standard", "Large"]}
        ]
        assert len(sent["variants"]) == 2
        # No variant-level PUT — variants replaced entirely via the product PUT.
        variant_put_paths = [c.args[0] for c in put_calls if c.args[0].startswith("variants/")]
        assert variant_put_paths == []

    async def test_active_meal_is_published_everywhere(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        sample_meal["is_active_today"] = True
        mock_dest_client.get = AsyncMock(return_value={"products": []})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql())
        mock_dest_client.post = AsyncMock(return_value={"product": {"id": 9001}})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        # publishablePublish must be issued with both publication GIDs
        gql_calls = mock_dest_client.graphql.await_args_list
        publish_calls = [c for c in gql_calls if "publishablePublish" in c.args[0]]
        assert len(publish_calls) == 1
        publish_input = publish_calls[0].kwargs["variables"]["input"]
        pub_gids = sorted(i["publicationId"] for i in publish_input)
        assert pub_gids == [
            "gid://shopify/Publication/1", "gid://shopify/Publication/2"
        ]
        # No unpublish
        assert not any("publishableUnpublish" in c.args[0] for c in gql_calls)
        # `published` REST flag is True on create
        sent = mock_dest_client.post.await_args.args[1]["product"]
        assert sent["published"] is True

    async def test_inactive_meal_is_unpublished_everywhere(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        sample_meal["is_active_today"] = False
        mock_dest_client.get = AsyncMock(return_value={"products": []})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql())
        mock_dest_client.post = AsyncMock(return_value={"product": {"id": 9001}})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        gql_calls = mock_dest_client.graphql.await_args_list
        unpublish_calls = [c for c in gql_calls if "publishableUnpublish" in c.args[0]]
        assert len(unpublish_calls) == 1
        # No publish
        assert not any("publishablePublish" in c.args[0] for c in gql_calls)
        # `published` REST flag is False on create
        sent = mock_dest_client.post.await_args.args[1]["product"]
        assert sent["published"] is False

    async def test_publications_fetched_once_across_meals(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        # Two meals → publication list still queried only once (cached)
        meal2 = dict(sample_meal); meal2["meal_id"] = 43; meal2["name"] = "Other meal"
        mock_dest_client.get = AsyncMock(return_value={"products": []})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql())
        mock_dest_client.post = AsyncMock(return_value={"product": {"id": 9001}})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal, meal2]))
        await resource.load()

        gql_calls = mock_dest_client.graphql.await_args_list
        list_pub_calls = [c for c in gql_calls if "listPublications" in c.args[0]]
        assert len(list_pub_calls) == 1

    async def test_failure_is_isolated(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        # Handle lookup raises for the first meal, returns empty for the second
        mock_dest_client.get = AsyncMock(side_effect=[
            RuntimeError("boom"),
            {"products": []},
        ])
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql())
        mock_dest_client.post = AsyncMock(return_value={"product": {"id": 9001}})

        meal2 = dict(sample_meal); meal2["meal_id"] = 43; meal2["name"] = "Other meal"
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal, meal2]))

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        await resource.load()

        # First failed, second created → POST called once
        mock_dest_client.post.assert_awaited_once()
        # Failure was logged
        failed_entries = json.loads(
            failed_log._path.read_text()
        ) if failed_log._path.exists() else []
        assert any(e["resource_type"] == "meals" for e in failed_entries)

    async def test_associates_product_with_subscription_group(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        mock_dest_client.get = AsyncMock(return_value={"products": []})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql())
        mock_dest_client.post = AsyncMock(return_value={"product": {"id": 9001}})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        gql_calls = mock_dest_client.graphql.await_args_list
        add_calls = [
            c for c in gql_calls if "sellingPlanGroupAddProducts" in c.args[0]
        ]
        assert len(add_calls) == 1
        vars_ = add_calls[0].kwargs["variables"]
        assert vars_["id"] == "gid://shopify/SellingPlanGroup/500"
        assert vars_["productIds"] == ["gid://shopify/Product/9001"]

    async def test_subscription_group_lookup_is_cached(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        meal2 = dict(sample_meal); meal2["meal_id"] = 43; meal2["name"] = "Other meal"
        mock_dest_client.get = AsyncMock(return_value={"products": []})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql())
        mock_dest_client.post = AsyncMock(return_value={"product": {"id": 9001}})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal, meal2]))
        await resource.load()

        gql_calls = mock_dest_client.graphql.await_args_list
        lookups = [c for c in gql_calls if "findSellingPlanGroup" in c.args[0]]
        assert len(lookups) == 1

    async def test_subscription_group_not_found_skips_association(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        # Override: the lookup returns no matching SPG. The meal still syncs
        # successfully; the SPG-add mutation never fires.
        empty_lookup = {"sellingPlanGroups": {"edges": []}}
        mock_dest_client.get = AsyncMock(return_value={"products": []})
        mock_dest_client.graphql = AsyncMock(
            side_effect=_smart_graphql(selling_plan_group_response=empty_lookup)
        )
        mock_dest_client.post = AsyncMock(return_value={"product": {"id": 9001}})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        gql_calls = mock_dest_client.graphql.await_args_list
        adds = [c for c in gql_calls if "sellingPlanGroupAddProducts" in c.args[0]]
        assert adds == []
        # The product was still created
        mock_dest_client.post.assert_awaited()

    async def test_empty_subscription_code_disables_association(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        # Operator unsets SHOPIFY_SUBSCRIPTION_GROUP_CODE → no lookup, no add.
        mock_dest_client.get = AsyncMock(return_value={"products": []})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql())
        mock_dest_client.post = AsyncMock(return_value={"product": {"id": 9001}})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer,
            subscription_group_code="",
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        gql_calls = mock_dest_client.graphql.await_args_list
        assert not any("findSellingPlanGroup" in c.args[0] for c in gql_calls)
        assert not any(
            "sellingPlanGroupAddProducts" in c.args[0] for c in gql_calls
        )

    # ── Conditional update path (diff-gated) ─────────────────────────────────

    async def _matching_existing(self, sample_meal, *, image_sources=None):
        """Build an existing graphql-node that exactly matches what `transform`
        would send for `sample_meal` — so all four diffs are empty."""
        # Render the payload once to capture body_html (Jinja-rendered).
        resource = MealsResource(
            source_client=None, dest_client=AsyncMock(),
            data_dir=Path("/tmp"), id_map=IDMap(Path("/tmp/x.json")),
            progress=AsyncMock(), failed_log=AsyncMock(),
            dry_run=True, renderer=DescriptionRenderer(TEMPLATE_PATH),
            django_dsn="postgres://unused", subscription_group_code="main-bundle",
        )
        payload = resource.transform(sample_meal)
        srcs = (
            sample_meal.get("image_urls") or []
            if image_sources is None else image_sources
        )
        # Mirror the managed metafields the resource would write, so an
        # otherwise-unchanged meal produces an empty metafield diff. Native
        # shopify.* references aren't included (sample_meal has no structured
        # recipe data, and the resolver is a no-op without read_metaobjects).
        managed = {
            f"{mf['namespace']}.{mf['key']}": mf["value"]
            for mf in _build_managed_metafields(sample_meal)
        }
        return _existing_node(
            payload=payload,
            image_sources=srcs,
            publications={"gid://shopify/Publication/1": True,
                          "gid://shopify/Publication/2": True},
            selling_plan_group_ids=["gid://shopify/SellingPlanGroup/500"],
            category_gid=sample_meal.get("category_gid"),
            managed_metafields=managed,
        )

    async def test_unchanged_meal_makes_no_writes(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        existing = await self._matching_existing(sample_meal)
        mock_dest_client.get = AsyncMock(return_value={"products": [{
            "id": 9001, "admin_graphql_api_id": "gid://shopify/Product/9001",
            "title": sample_meal["name"], "handle": _slugify(sample_meal["name"]),
        }]})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql(
            get_product_response=existing,
        ))
        mock_dest_client.put = AsyncMock(return_value={})
        mock_dest_client.delete = AsyncMock(return_value=None)
        mock_dest_client.post = AsyncMock(return_value={})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        # Zero PUTs, zero POSTs, zero DELETEs.
        mock_dest_client.put.assert_not_awaited()
        mock_dest_client.post.assert_not_awaited()
        mock_dest_client.delete.assert_not_awaited()
        # No publish/unpublish and no SPG-add either.
        gql_calls = mock_dest_client.graphql.await_args_list
        assert not any("publishablePublish" in c.args[0] for c in gql_calls)
        assert not any("publishableUnpublish" in c.args[0] for c in gql_calls)
        assert not any("sellingPlanGroupAddProducts" in c.args[0] for c in gql_calls)

    async def test_only_body_html_change_sends_sparse_put(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        existing = await self._matching_existing(sample_meal)
        existing["bodyHtml"] = "<p>stale description</p>"  # only this differs
        mock_dest_client.get = AsyncMock(return_value={"products": [{
            "id": 9001, "admin_graphql_api_id": "gid://shopify/Product/9001",
            "title": sample_meal["name"], "handle": _slugify(sample_meal["name"]),
        }]})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql(
            get_product_response=existing,
        ))
        mock_dest_client.put = AsyncMock(return_value={})
        mock_dest_client.delete = AsyncMock(return_value=None)
        mock_dest_client.post = AsyncMock(return_value={})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        put_calls = mock_dest_client.put.await_args_list
        assert len(put_calls) == 1
        sent = put_calls[0].args[1]["product"]
        # Sparse PUT: only `id` and `body_html`, nothing else.
        assert set(sent.keys()) == {"id", "body_html"}
        assert "fresheo-product" in sent["body_html"]
        # No image refresh, no publish, no SPG-add.
        mock_dest_client.delete.assert_not_awaited()
        gql_calls = mock_dest_client.graphql.await_args_list
        assert not any("publishablePublish" in c.args[0] for c in gql_calls)
        assert not any("sellingPlanGroupAddProducts" in c.args[0] for c in gql_calls)

    async def test_image_only_change_triggers_refresh_and_metafield_write(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        existing = await self._matching_existing(
            sample_meal, image_sources=["https://cdn.example.com/old.jpg"],
        )
        existing["images"] = {"edges": [
            {"node": {"id": "gid://shopify/ProductImage/111"}},
        ]}
        mock_dest_client.get = AsyncMock(return_value={"products": [{
            "id": 9001, "admin_graphql_api_id": "gid://shopify/Product/9001",
            "title": sample_meal["name"], "handle": _slugify(sample_meal["name"]),
        }]})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql(
            get_product_response=existing,
        ))
        mock_dest_client.put = AsyncMock(return_value={})
        mock_dest_client.delete = AsyncMock(return_value=None)
        mock_dest_client.post = AsyncMock(return_value={})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        # No product body PUT (body fields all match).
        mock_dest_client.put.assert_not_awaited()
        # One image DELETE + one image POST + one metafield POST.
        assert mock_dest_client.delete.await_count == 1
        post_paths = [c.args[0] for c in mock_dest_client.post.await_args_list]
        assert "products/9001/images.json" in post_paths
        assert "products/9001/metafields.json" in post_paths

    async def test_publication_state_already_correct_skips_publish(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        # All other fields match; publications already in target state → no
        # publish/unpublish mutation fires.
        existing = await self._matching_existing(sample_meal)
        mock_dest_client.get = AsyncMock(return_value={"products": [{
            "id": 9001, "admin_graphql_api_id": "gid://shopify/Product/9001",
            "title": sample_meal["name"], "handle": _slugify(sample_meal["name"]),
        }]})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql(
            get_product_response=existing,
        ))
        mock_dest_client.put = AsyncMock(return_value={})
        mock_dest_client.delete = AsyncMock(return_value=None)
        mock_dest_client.post = AsyncMock(return_value={})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        gql_calls = mock_dest_client.graphql.await_args_list
        assert not any("publishablePublish" in c.args[0] for c in gql_calls)
        assert not any("publishableUnpublish" in c.args[0] for c in gql_calls)

    async def test_publication_flip_only_targets_changed_publications(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        # Meal was inactive; existing has all pubs unpublished. Now active —
        # publish mutation should target every publication.
        existing = await self._matching_existing(sample_meal)
        existing["resourcePublicationsV2"] = {"edges": [
            {"node": {"publication": {"id": "gid://shopify/Publication/1"},
                      "isPublished": False}},
            {"node": {"publication": {"id": "gid://shopify/Publication/2"},
                      "isPublished": True}},  # already published here
        ]}
        mock_dest_client.get = AsyncMock(return_value={"products": [{
            "id": 9001, "admin_graphql_api_id": "gid://shopify/Product/9001",
            "title": sample_meal["name"], "handle": _slugify(sample_meal["name"]),
        }]})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql(
            get_product_response=existing,
        ))
        mock_dest_client.put = AsyncMock(return_value={})
        mock_dest_client.delete = AsyncMock(return_value=None)
        mock_dest_client.post = AsyncMock(return_value={})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        gql_calls = mock_dest_client.graphql.await_args_list
        publish_calls = [c for c in gql_calls if "publishablePublish" in c.args[0]]
        # Exactly one publish mutation targeting only Publication/1.
        assert len(publish_calls) == 1
        input_value = publish_calls[0].kwargs["variables"]["input"]
        assert input_value == [{"publicationId": "gid://shopify/Publication/1"}]

    async def test_spg_already_associated_skips_add(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        existing = await self._matching_existing(sample_meal)
        mock_dest_client.get = AsyncMock(return_value={"products": [{
            "id": 9001, "admin_graphql_api_id": "gid://shopify/Product/9001",
            "title": sample_meal["name"], "handle": _slugify(sample_meal["name"]),
        }]})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql(
            get_product_response=existing,
        ))
        mock_dest_client.put = AsyncMock(return_value={})
        mock_dest_client.delete = AsyncMock(return_value=None)
        mock_dest_client.post = AsyncMock(return_value={})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        gql_calls = mock_dest_client.graphql.await_args_list
        assert not any(
            "sellingPlanGroupAddProducts" in c.args[0] for c in gql_calls
        )

    async def test_create_links_every_variant_to_product_image(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        # POST response carries the freshly-assigned image and variant IDs;
        # _create issues a follow-up PUT on the image with `variant_ids` so
        # every variant page shows the same picture.
        mock_dest_client.get = AsyncMock(return_value={"products": []})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql())
        mock_dest_client.post = AsyncMock(return_value={"product": {
            "id": 9001,
            "images":   [{"id": 777, "src": "https://cdn.shopify/x.jpg"}],
            "variants": [{"id": 5001}, {"id": 5002}],
        }})
        mock_dest_client.put = AsyncMock(return_value={})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        put_calls = mock_dest_client.put.await_args_list
        link_puts = [c for c in put_calls if c.args[0] == "products/9001/images/777.json"]
        assert len(link_puts) == 1
        body = link_puts[0].args[1]["image"]
        assert body["id"] == 777
        assert sorted(body["variant_ids"]) == [5001, 5002]

    async def test_create_disables_inventory_tracking_on_variants(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        # POST returns the new variant IDs; _create must follow up with a
        # productVariantsBulkUpdate setting inventoryItem.tracked=false on each
        # — the REST inventory_management field is a no-op on API 2024-04+.
        mock_dest_client.get = AsyncMock(return_value={"products": []})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql())
        mock_dest_client.post = AsyncMock(return_value={"product": {
            "id": 9001,
            "variants": [{"id": 5001}, {"id": 5002}],
        }})
        mock_dest_client.put = AsyncMock(return_value={})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        gql_calls = mock_dest_client.graphql.await_args_list
        disable_calls = [
            c for c in gql_calls if "productVariantsBulkUpdate" in c.args[0]
        ]
        assert len(disable_calls) == 1
        vars_ = disable_calls[0].kwargs["variables"]
        assert vars_["productId"] == "gid://shopify/Product/9001"
        assert vars_["variants"] == [
            {"id": "gid://shopify/ProductVariant/5001",
             "inventoryItem": {"tracked": False}},
            {"id": "gid://shopify/ProductVariant/5002",
             "inventoryItem": {"tracked": False}},
        ]

    async def test_existing_tracked_variant_disabled_without_other_writes(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        # An already-synced product whose variants are still (wrongly) tracked.
        # Nothing else differs → no PUT/POST/DELETE, but a productVariantsBulkUpdate
        # must fire to flip tracking off on the existing variant IDs.
        existing = await self._matching_existing(sample_meal)
        for edge in existing["variants"]["edges"]:
            edge["node"]["inventoryItem"]["tracked"] = True
        mock_dest_client.get = AsyncMock(return_value={"products": [{
            "id": 9001, "admin_graphql_api_id": "gid://shopify/Product/9001",
            "title": sample_meal["name"], "handle": _slugify(sample_meal["name"]),
        }]})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql(
            get_product_response=existing,
        ))
        mock_dest_client.put = AsyncMock(return_value={})
        mock_dest_client.delete = AsyncMock(return_value=None)
        mock_dest_client.post = AsyncMock(return_value={})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        # Tracking is its own concern — no body PUT, no image writes.
        mock_dest_client.put.assert_not_awaited()
        mock_dest_client.post.assert_not_awaited()
        mock_dest_client.delete.assert_not_awaited()
        # Disabled on the existing variant IDs (5001, 5002 per _existing_node).
        gql_calls = mock_dest_client.graphql.await_args_list
        disable_calls = [
            c for c in gql_calls if "productVariantsBulkUpdate" in c.args[0]
        ]
        assert len(disable_calls) == 1
        sent = disable_calls[0].kwargs["variables"]["variants"]
        assert [v["id"] for v in sent] == [
            "gid://shopify/ProductVariant/5001",
            "gid://shopify/ProductVariant/5002",
        ]
        assert all(v["inventoryItem"] == {"tracked": False} for v in sent)

    async def test_untracked_existing_variant_skips_disable(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        # Steady state: existing variants already untracked and nothing changed
        # → fully idempotent, no productVariantsBulkUpdate (no re-PUT loop).
        existing = await self._matching_existing(sample_meal)  # tracked=False
        mock_dest_client.get = AsyncMock(return_value={"products": [{
            "id": 9001, "admin_graphql_api_id": "gid://shopify/Product/9001",
            "title": sample_meal["name"], "handle": _slugify(sample_meal["name"]),
        }]})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql(
            get_product_response=existing,
        ))
        mock_dest_client.put = AsyncMock(return_value={})
        mock_dest_client.delete = AsyncMock(return_value=None)
        mock_dest_client.post = AsyncMock(return_value={})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        gql_calls = mock_dest_client.graphql.await_args_list
        assert not any(
            "productVariantsBulkUpdate" in c.args[0] for c in gql_calls
        )

    async def test_create_sets_taxonomy_category(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        # MAIN_DISH → fb-2-15-2; create must follow up with a productUpdate
        # carrying that taxonomy GID (REST create can't set the category).
        mock_dest_client.get = AsyncMock(return_value={"products": []})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql())
        mock_dest_client.post = AsyncMock(return_value={"product": {"id": 9001}})
        mock_dest_client.put = AsyncMock(return_value={})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        gql_calls = mock_dest_client.graphql.await_args_list
        cat_calls = [c for c in gql_calls if "setCategory" in c.args[0]]
        assert len(cat_calls) == 1
        product = cat_calls[0].kwargs["variables"]["product"]
        assert product == {
            "id": "gid://shopify/Product/9001",
            "category": "gid://shopify/TaxonomyCategory/fb-2-15-2",
        }

    async def test_unmapped_category_skips_productupdate_on_create(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        # VACUUM is deliberately uncategorized (→ 21%). No category GID means no
        # productUpdate — the product is left uncategorized, never cleared.
        sample_meal["category"] = "VACUUM"
        sample_meal["category_gid"] = None
        mock_dest_client.get = AsyncMock(return_value={"products": []})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql())
        mock_dest_client.post = AsyncMock(return_value={"product": {"id": 9001}})
        mock_dest_client.put = AsyncMock(return_value={})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        gql_calls = mock_dest_client.graphql.await_args_list
        assert not any("setCategory" in c.args[0] for c in gql_calls)

    async def test_update_sets_category_when_existing_differs(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        # Already-synced product with no category assigned. Everything else
        # matches → only a productUpdate to assign the taxonomy node fires.
        existing = await self._matching_existing(sample_meal)
        existing["category"] = None  # not yet categorized
        mock_dest_client.get = AsyncMock(return_value={"products": [{
            "id": 9001, "admin_graphql_api_id": "gid://shopify/Product/9001",
            "title": sample_meal["name"], "handle": _slugify(sample_meal["name"]),
        }]})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql(
            get_product_response=existing,
        ))
        mock_dest_client.put = AsyncMock(return_value={})
        mock_dest_client.delete = AsyncMock(return_value=None)
        mock_dest_client.post = AsyncMock(return_value={})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        # Category is its own concern — no body PUT, no image writes.
        mock_dest_client.put.assert_not_awaited()
        mock_dest_client.post.assert_not_awaited()
        gql_calls = mock_dest_client.graphql.await_args_list
        cat_calls = [c for c in gql_calls if "setCategory" in c.args[0]]
        assert len(cat_calls) == 1
        assert cat_calls[0].kwargs["variables"]["product"]["category"] == (
            "gid://shopify/TaxonomyCategory/fb-2-15-2"
        )

    async def test_update_skips_category_when_already_correct(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        # Existing already carries the right taxonomy node → idempotent, no
        # productUpdate (no re-write loop).
        existing = await self._matching_existing(sample_meal)  # category matches
        mock_dest_client.get = AsyncMock(return_value={"products": [{
            "id": 9001, "admin_graphql_api_id": "gid://shopify/Product/9001",
            "title": sample_meal["name"], "handle": _slugify(sample_meal["name"]),
        }]})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql(
            get_product_response=existing,
        ))
        mock_dest_client.put = AsyncMock(return_value={})
        mock_dest_client.delete = AsyncMock(return_value=None)
        mock_dest_client.post = AsyncMock(return_value={})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        gql_calls = mock_dest_client.graphql.await_args_list
        assert not any("setCategory" in c.args[0] for c in gql_calls)

    async def test_create_skips_link_when_no_variants_in_response(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        # Defensive: if Shopify response omits variants (shouldn't happen in
        # practice), don't crash — just don't issue the link PUT.
        mock_dest_client.get = AsyncMock(return_value={"products": []})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql())
        mock_dest_client.post = AsyncMock(return_value={"product": {
            "id": 9001, "images": [{"id": 777}],
        }})
        mock_dest_client.put = AsyncMock(return_value={})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        link_puts = [c for c in mock_dest_client.put.await_args_list
                     if c.args[0].startswith("products/9001/images/")]
        assert link_puts == []

    async def test_image_refresh_links_new_image_to_existing_variants(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        # Image-only change → new image POST carries variant_ids drawn from
        # the existing variant set (variants didn't change in this scenario).
        existing = await self._matching_existing(
            sample_meal, image_sources=["https://cdn.example.com/old.jpg"],
        )
        existing["images"] = {"edges": [
            {"node": {"id": "gid://shopify/ProductImage/111"}},
        ]}
        mock_dest_client.get = AsyncMock(return_value={"products": [{
            "id": 9001, "admin_graphql_api_id": "gid://shopify/Product/9001",
            "title": sample_meal["name"], "handle": _slugify(sample_meal["name"]),
        }]})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql(
            get_product_response=existing,
        ))
        mock_dest_client.put = AsyncMock(return_value={})
        mock_dest_client.delete = AsyncMock(return_value=None)
        mock_dest_client.post = AsyncMock(return_value={})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        post_calls = mock_dest_client.post.await_args_list
        image_posts = [c for c in post_calls if c.args[0] == "products/9001/images.json"]
        assert len(image_posts) == 1
        body = image_posts[0].args[1]["image"]
        assert "variant_ids" in body
        # `_existing_node` auto-assigns legacyResourceId 5001+i per variant.
        assert sorted(body["variant_ids"]) == [5001, 5002]

    async def test_image_refresh_uses_post_put_variant_ids_when_variants_replaced(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        # Both body (variants) and images differ → sparse PUT replaces variants,
        # then image POSTs use the NEW variant IDs from the PUT response, not
        # the stale pre-update IDs.
        existing = await self._matching_existing(
            sample_meal, image_sources=["https://cdn.example.com/old.jpg"],
        )
        # Force a variant diff by giving existing stale variant prices.
        for v in existing["variants"]["edges"]:
            v["node"]["price"] = "0.01"
        existing["images"] = {"edges": [
            {"node": {"id": "gid://shopify/ProductImage/111"}},
        ]}

        mock_dest_client.get = AsyncMock(return_value={"products": [{
            "id": 9001, "admin_graphql_api_id": "gid://shopify/Product/9001",
            "title": sample_meal["name"], "handle": _slugify(sample_meal["name"]),
        }]})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql(
            get_product_response=existing,
        ))
        # PUT returns the new variant IDs Shopify assigned after the replace.
        mock_dest_client.put = AsyncMock(return_value={"product": {
            "id": 9001,
            "variants": [{"id": 6001}, {"id": 6002}],
        }})
        mock_dest_client.delete = AsyncMock(return_value=None)
        mock_dest_client.post = AsyncMock(return_value={})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        post_calls = mock_dest_client.post.await_args_list
        image_posts = [c for c in post_calls if c.args[0] == "products/9001/images.json"]
        assert len(image_posts) == 1
        body = image_posts[0].args[1]["image"]
        assert sorted(body["variant_ids"]) == [6001, 6002]

    async def test_spg_missing_triggers_add(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        # Existing has no SPG associations → diff fires SPG add even when all
        # body fields match.
        existing = await self._matching_existing(sample_meal)
        existing["sellingPlanGroups"] = {"edges": []}
        mock_dest_client.get = AsyncMock(return_value={"products": [{
            "id": 9001, "admin_graphql_api_id": "gid://shopify/Product/9001",
            "title": sample_meal["name"], "handle": _slugify(sample_meal["name"]),
        }]})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql(
            get_product_response=existing,
        ))
        mock_dest_client.put = AsyncMock(return_value={})
        mock_dest_client.delete = AsyncMock(return_value=None)
        mock_dest_client.post = AsyncMock(return_value={})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        gql_calls = mock_dest_client.graphql.await_args_list
        adds = [c for c in gql_calls if "sellingPlanGroupAddProducts" in c.args[0]]
        assert len(adds) == 1
        # Product body unchanged → no PUT.
        mock_dest_client.put.assert_not_awaited()


# ── Metafield builders (django_db) ────────────────────────────────────────────


class TestBuildCooking:
    def test_microwave_and_oven(self):
        row = {"cold_meal": False, "microwave_cooking_power": 900,
               "microwave_cooking_time": "3 min 30",
               "oven_cooking_temp": 175, "oven_cooking_time": "12 min"}
        assert _build_cooking(row) == {
            "cold": False,
            "microwave": {"power_w": 900, "time": "3 min 30"},
            "oven": {"temp_c": 175, "time": "12 min"},
        }

    def test_cold_meal_without_heating(self):
        row = {"cold_meal": True, "microwave_cooking_power": 0,
               "microwave_cooking_time": "", "oven_cooking_temp": 0,
               "oven_cooking_time": ""}
        assert _build_cooking(row) == {"cold": True}

    def test_nothing_to_say_returns_none(self):
        row = {"cold_meal": False, "microwave_cooking_power": 0,
               "microwave_cooking_time": "", "oven_cooking_temp": 0,
               "oven_cooking_time": ""}
        assert _build_cooking(row) is None

    def test_microwave_only_omits_oven(self):
        row = {"cold_meal": False, "microwave_cooking_power": 800,
               "microwave_cooking_time": "4 min", "oven_cooking_temp": 0,
               "oven_cooking_time": ""}
        cooking = _build_cooking(row)
        assert cooking == {"cold": False,
                           "microwave": {"power_w": 800, "time": "4 min"}}


class TestBuildRecipeFlags:
    def test_none_when_no_recipe(self):
        assert _build_recipe_flags({"recipe_id": None}, ["gluten"], "allergen_") is None

    def test_collects_truthy_columns_in_order(self):
        row = {"recipe_id": 5, "allergen_gluten": True, "allergen_milk": True,
               "allergen_eggs": False}
        assert _build_recipe_flags(
            row, ["gluten", "eggs", "milk"], "allergen_"
        ) == ["gluten", "milk"]

    def test_recipe_with_no_flags_is_empty_list_not_none(self):
        # Recipe exists but flags all false → [] (distinguished from None).
        row = {"recipe_id": 5, "allergen_gluten": False}
        assert _build_recipe_flags(row, ["gluten"], "allergen_") == []


class TestMealTypeTag:
    def test_slugifies_with_menu_prefix(self):
        assert _meal_type_tag("Végétarien Gourmand") == "menu-vegetarien-gourmand"

    def test_empty_returns_none(self):
        assert _meal_type_tag("") is None
        assert _meal_type_tag(None) is None  # type: ignore[arg-type]


class TestBuildTagsMealType:
    def _diets(self) -> dict[str, bool]:
        return {s: False for s in [
            "vegetarien", "vegan", "sans-gluten", "sans-lactose",
            "sans-porc", "meat", "fish", "fitness",
        ]}

    def test_meal_type_tag_included(self):
        tags = _build_tags(diets=self._diets(), is_active_today=False,
                           category=None, nutri_score=None, meal_type="Fitness")
        assert "menu-fitness" in tags

    def test_no_meal_type_no_menu_tag(self):
        tags = _build_tags(diets=self._diets(), is_active_today=False,
                           category=None, nutri_score=None)
        assert not any(t.startswith("menu-") for t in tags)


# ── Metafield helpers (meals) ─────────────────────────────────────────────────


class TestNutritionPayload:
    def test_builds_from_macros(self):
        payload = _nutrition_payload(
            {"kilo_calories": 254, "proteins": 32.5, "weight": 450}
        )
        assert payload["kilo_calories"] == 254
        assert payload["proteins"] == 32.5
        assert payload["weight"] == 450
        assert payload["salts"] is None  # absent macro present as None

    def test_all_zero_or_missing_returns_none(self):
        assert _nutrition_payload({}) is None
        assert _nutrition_payload({"kilo_calories": 0, "proteins": 0}) is None


class TestBuildManagedMetafields:
    def test_nutri_and_nutrition_present(self):
        mfs = _build_managed_metafields(
            {"nutri_score": "A", "kilo_calories": 254, "proteins": 30}
        )
        keys = {(m["namespace"], m["key"]) for m in mfs}
        assert ("fresheo", "nutri_score") in keys
        assert ("fresheo", "nutrition") in keys

    def test_cooking_and_author_when_present(self):
        mfs = {m["key"]: m for m in _build_managed_metafields(
            {"nutri_score": "B", "cooking": {"cold": True}, "author": "gusto"}
        )}
        assert mfs["cooking_instructions"]["type"] == "json"
        assert mfs["author"]["value"] == "gusto"

    def test_blank_sources_skipped(self):
        keys = {m["key"] for m in _build_managed_metafields(
            {"nutri_score": "", "author": "  ", "cooking": None}
        )}
        assert keys == set()


class TestCanonicalMetafieldValue:
    def test_json_key_order_insensitive(self):
        assert (
            _canonical_metafield_value('{"b":1,"a":2}', "json")
            == _canonical_metafield_value('{"a":2,"b":1}', "json")
        )

    def test_reference_list_order_insensitive(self):
        assert (
            _canonical_metafield_value('["gid://x/2","gid://x/1"]',
                                       "list.metaobject_reference")
            == _canonical_metafield_value('["gid://x/1","gid://x/2"]',
                                          "list.metaobject_reference")
        )

    def test_text_raw_and_empty(self):
        assert _canonical_metafield_value("A", "single_line_text_field") == "A"
        assert _canonical_metafield_value("", "json") == ""
        assert _canonical_metafield_value(None, "json") == ""


class TestDiffMetafields:
    def _mf(self, value="A", key="nutri_score", mtype="single_line_text_field"):
        return {"namespace": "fresheo", "key": key, "type": mtype, "value": value}

    def test_no_change_when_values_match(self):
        assert _diff_metafields({"fresheo.nutri_score": "A"}, [self._mf("A")]) == []

    def test_change_detected(self):
        assert len(_diff_metafields({"fresheo.nutri_score": "B"}, [self._mf("A")])) == 1

    def test_missing_existing_is_a_change(self):
        desired = [self._mf("A")]
        assert _diff_metafields({}, desired) == desired

    def test_json_formatting_diff_ignored(self):
        existing = {"fresheo.nutrition": '{"a": 1, "b": 2}'}
        desired = [self._mf('{"b":2,"a":1}', key="nutrition", mtype="json")]
        assert _diff_metafields(existing, desired) == []


class TestMatchReference:
    def test_matches_normalized_handle(self):
        assert _match_reference({"tree-nuts": "gid://x/9"}, ["tree_nuts"]) == "gid://x/9"

    def test_first_candidate_wins(self):
        assert _match_reference({"dairy": "gid://x/5"}, ["milk", "dairy"]) == "gid://x/5"

    def test_no_match_returns_none(self):
        assert _match_reference({"gluten": "g"}, ["soy"]) is None


class TestNormalizeReferenceToken:
    def test_collapses_separators(self):
        assert _normalize_reference_token("Tree Nuts") == "tree-nuts"
        assert _normalize_reference_token("tree_nuts") == "tree-nuts"
        assert _normalize_reference_token("tree-nuts") == "tree-nuts"


# ── Metafield sync (integration) ──────────────────────────────────────────────


class TestMealMetafieldSync:
    async def test_create_writes_managed_metafields(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        mock_dest_client.get = AsyncMock(return_value={"products": []})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql())
        mock_dest_client.post = AsyncMock(return_value={"product": {"id": 9001}})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        set_calls = [
            c for c in mock_dest_client.graphql.await_args_list
            if "setMetafields" in c.args[0]
        ]
        assert len(set_calls) == 1
        sent = set_calls[0].kwargs["variables"]["metafields"]
        keys = {(m["namespace"], m["key"]) for m in sent}
        assert ("fresheo", "nutri_score") in keys
        assert ("fresheo", "nutrition") in keys
        assert all(m["ownerId"] == "gid://shopify/Product/9001" for m in sent)

    async def test_unchanged_metafields_skip_write(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        # All managed metafields already match → no metafieldsSet on update.
        existing = await TestMealsLoad()._matching_existing(sample_meal)
        mock_dest_client.get = AsyncMock(return_value={"products": [{
            "id": 9001, "admin_graphql_api_id": "gid://shopify/Product/9001",
            "title": sample_meal["name"], "handle": _slugify(sample_meal["name"]),
        }]})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql(
            get_product_response=existing,
        ))
        mock_dest_client.put = AsyncMock(return_value={})
        mock_dest_client.delete = AsyncMock(return_value=None)
        mock_dest_client.post = AsyncMock(return_value={})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        assert not any(
            "setMetafields" in c.args[0]
            for c in mock_dest_client.graphql.await_args_list
        )

    async def test_native_allergens_resolved_when_scope_present(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        # Structured recipe allergens + a readable reference map → the native
        # shopify.allergen-information metafield is written with metaobject GIDs.
        sample_meal["allergens_struct"] = ["gluten", "milk"]
        mock_dest_client.get = AsyncMock(return_value={"products": []})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql(
            metafield_definition_response={"metafieldDefinitions": {"edges": [
                {"node": {"id": "gid://shopify/MetafieldDefinition/1",
                          "validations": [{"name": "metaobject_definition_id",
                                           "value": "gid://shopify/MetaobjectDefinition/100"}]}},
            ]}},
            metaobject_definition_response={"metaobjectDefinition": {"type": "shopify--allergen"}},
            metaobjects_response={"metaobjects": {
                "edges": [
                    {"node": {"id": "gid://shopify/Metaobject/11",
                              "handle": "gluten", "displayName": "Gluten"}},
                    {"node": {"id": "gid://shopify/Metaobject/12",
                              "handle": "milk", "displayName": "Milk"}},
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }},
        ))
        mock_dest_client.post = AsyncMock(return_value={"product": {"id": 9001}})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        set_calls = [
            c for c in mock_dest_client.graphql.await_args_list
            if "setMetafields" in c.args[0]
        ]
        assert len(set_calls) == 1
        sent = {(m["namespace"], m["key"]): m for m in set_calls[0].kwargs["variables"]["metafields"]}
        allergen_mf = sent[("shopify", "allergen-information")]
        assert allergen_mf["type"] == "list.metaobject_reference"
        assert json.loads(allergen_mf["value"]) == [
            "gid://shopify/Metaobject/11", "gid://shopify/Metaobject/12",
        ]

    async def test_native_allergens_skipped_without_scope(
        self, mock_dest_client, tmp_data_dir, progress, failed_log, renderer, sample_meal
    ):
        # Structured allergens present but the reference metafield definition
        # isn't readable (default _smart_graphql) → native metafield is omitted,
        # plain fresheo.* metafields still written.
        sample_meal["allergens_struct"] = ["gluten", "milk"]
        mock_dest_client.get = AsyncMock(return_value={"products": []})
        mock_dest_client.graphql = AsyncMock(side_effect=_smart_graphql())
        mock_dest_client.post = AsyncMock(return_value={"product": {"id": 9001}})

        resource = _make_resource(
            mock_dest_client, tmp_data_dir, progress, failed_log, renderer
        )
        (tmp_data_dir / "meals.json").write_text(json.dumps([sample_meal]))
        await resource.load()

        set_calls = [
            c for c in mock_dest_client.graphql.await_args_list
            if "setMetafields" in c.args[0]
        ]
        assert len(set_calls) == 1
        keys = {(m["namespace"], m["key"])
                for m in set_calls[0].kwargs["variables"]["metafields"]}
        assert ("shopify", "allergen-information") not in keys
        assert ("fresheo", "nutri_score") in keys

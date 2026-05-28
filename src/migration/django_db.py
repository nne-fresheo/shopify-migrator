from __future__ import annotations

import logging
import re
from datetime import date
from typing import Optional
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)


_MEALS_SQL = """
SELECT
    m.id                                AS meal_id,
    m.name                              AS name_raw,
    m.ingredients                       AS ingredients_raw,
    m.allergens                         AS allergens_raw,
    m.nutri_score                       AS nutri_score,
    m.filter_string                     AS filter_string,
    m.meal_image                        AS meal_image,
    m.picture                           AS picture_path,
    m.picture_webp                      AS picture_webp_path,
    m.category                          AS category,
    m.active_on                         AS active_on,
    m.inactive_on                       AS inactive_on,
    m.unit_price                        AS unit_price,
    m.extra_price                       AS extra_price,

    nv.weight, nv.kilo_calories, nv.proteins, nv.carbohydrates,
    nv.lipids, nv.sugars, nv.saturated, nv.fibers, nv.salts,

    COALESCE((SELECT AVG(r.rating)::float
              FROM account_review r WHERE r.meal_id = m.id), 5.0) AS avg_rating,
    (SELECT COUNT(*) FROM account_review r WHERE r.meal_id = m.id) AS rating_count
FROM menu_meal m
         LEFT JOIN LATERAL (
    SELECT *
    FROM menu_nutritionalvalues
    WHERE meal_id = m.id
    ORDER BY id ASC
    LIMIT 1
    ) nv ON TRUE
WHERE m.visible_for_customers = TRUE
  AND (m.inactive_on IS NULL OR m.inactive_on > CURRENT_DATE)
ORDER BY m.id;
"""


# Per-plan base prices. total_meals = 3 is the price tier used for variants;
# higher quantities are handled through Shopify promotions, not variant prices.
_PLANS_SQL = """
SELECT  p.id                    AS plan_id,
        p.name                  AS plan_name,
        mm.additional_meal_price AS additional_meal_price
FROM    menu_plan p
JOIN    menu_menu mm ON mm.plan_id = p.id AND mm.total_meals = 3
ORDER BY p.id;
"""


# Categories whose per-meal price scales with the selected plan (size). All
# other categories emit a single variant priced from unit_price + extra_price.
_SIZED_CATEGORIES = {"MAIN_DISH", "BREAKFAST"}


# Canonical Shopify variant labels per known plan name. Unknown plan names
# fall back to title-cased plan_name so a new plan in the DB still produces
# a sensible option value without code changes.
_PLAN_LABEL_ALIASES = {
    "standard": "Standard",
    "normale":  "Standard",
    "large":    "Large",
}


# Canonical badge order — also the iteration order in the rendered template.
_DIET_SLUGS = [
    "vegetarien", "vegan", "sans-gluten", "sans-lactose",
    "sans-porc", "meat", "fish", "fitness",
]

# Token → slug alias map. Tokens are matched after lowercasing the filter_string
# and splitting on whitespace, so casing variants and extra spaces are handled
# implicitly. Unknown tokens (paleo, first_pos, cold_meal, boisson, dessert,
# protein_rich, 1stpos, …) are silently ignored.
_TOKEN_TO_SLUG = {
    # canonical English (most rows)
    "vegetarian":   "vegetarien",
    "vegan":        "vegan",
    "gluten_free":  "sans-gluten",
    "lactose_free": "sans-lactose",
    "pork_free":    "sans-porc",
    "meat":         "meat",
    "fish":         "fish",
    "fitness":      "fitness",
    # French variants observed in the data
    "vegetarien":   "vegetarien",
    "végétarien":   "vegetarien",
    "vegetarién":   "vegetarien",
    # observed typos
    "lactos_free":  "sans-lactose",
    "factose_free": "sans-lactose",
    "lactose_":     "sans-lactose",   # from "lactose_ free" (space-broken)
}

_CATEGORY_LABELS_FR = {
    "MAIN_DISH": "Repas",
    "BREAKFAST": "Petit déjeuner",
    "DESSERT":   "Dessert",
    "DRINKS":    "Boissons",
    "VACUUM":    "Fresheo deals",
    "SNACK":     "Snacks",
    "NON_FOOD":  "Accessoires",
}


def category_label_fr(category: Optional[str]) -> str:
    """Translate a Meal.category enum value to its French label. Returns an
    empty string for unknown / missing values so the Shopify product_type
    falls back to empty rather than a placeholder."""
    if not category:
        return ""
    return _CATEGORY_LABELS_FR.get(category, "")


# Maps each Meal.category to a Shopify Standard Product Taxonomy node. This —
# NOT the free-text product_type — is what Shopify Tax reads to apply the
# Belgian VAT rate: food nodes resolve to 6%, and categories left unmapped fall
# back to the 21% standard rate. Confirmed with the user on 2026-05-25:
#   MAIN_DISH/BREAKFAST → Prepared Meals & Entrées, DESSERT → Prepared Desserts
#   & Sweets, SNACK → Snack Foods, DRINKS → Beverages (all 6%).
#   VACUUM ("Fresheo deals", undefined) and NON_FOOD are intentionally absent →
#   standard 21%.
# Values are bare taxonomy IDs; `category_taxonomy_gid` wraps them into the GID
# the GraphQL `category` field expects. The resulting rate must still be
# verified per category with a draft order — Shopify Tax's reduced-rate coverage
# is not uniform across nodes.
_CATEGORY_TAXONOMY = {
    "MAIN_DISH": "fb-2-15-2",   # Prepared Meals & Entrées
    "BREAKFAST": "fb-2-15-2",   # Prepared Meals & Entrées
    "DESSERT":   "fb-2-15-3",   # Prepared Desserts & Sweets
    "SNACK":     "fb-2-17",     # Snack Foods
    "DRINKS":    "fb-1",        # Beverages (non-alcoholic catalogue)
}


def category_taxonomy_gid(category: Optional[str]) -> Optional[str]:
    """Return the Shopify taxonomy category GID for a Meal.category, or None
    when the category is unmapped (VACUUM, NON_FOOD, unknown) — None means
    'leave uncategorized', which yields the 21% standard VAT rate."""
    node = _CATEGORY_TAXONOMY.get(category or "")
    return f"gid://shopify/TaxonomyCategory/{node}" if node else None


_DIET_LABELS_FR = {
    "vegetarien":   "🥗 Végétarien",
    "vegan":        "🌱 Vegan",
    "sans-gluten":  "🌾 Sans gluten",
    "sans-lactose": "🥛 Sans lactose",
    "sans-porc":    "🐷 Sans porc",
    "meat":         "🥩 Viande",
    "fish":         "🐟 Poisson",
    "fitness":      "💪 Fitness",
}


def _extract_locale(text: Optional[str], locale: str = "fr") -> str:
    """Port of fresheo.language_functions.tr_str.

    Finds all `<{locale}>...</{locale}>` substrings in text and joins them with
    spaces. Returns the raw text unchanged if no tags match.
    """
    if not text:
        return ""
    matches = re.findall(rf"<{locale}>(.+?)</{locale}>", text, flags=re.DOTALL)
    if not matches:
        return str(text)
    return " ".join(matches)


def _build_tags(
    *,
    diets: dict[str, bool],
    is_active_today: bool,
    category: Optional[str],
    nutri_score: Optional[str],
) -> list[str]:
    """Sorted Shopify tags for a meal.

    Includes: active diet slugs from [[_parse_diet_flags]], `current-menu`
    when the meal's active window includes today, the category enum kebab-cased
    (`MAIN_DISH` → `main-dish`), and `nutri-{a..e}`. Sorted for stable payloads
    so re-runs produce identical CSVs and avoid no-op PUTs flagging changes.
    """
    tags: set[str] = {slug for slug, on in (diets or {}).items() if on}
    if is_active_today:
        tags.add("current-menu")
    if category:
        tags.add(category.lower().replace("_", "-"))
    if nutri_score:
        tags.add(f"nutri-{nutri_score.lower()}")
    return sorted(tags)


def _parse_diet_flags(filter_string: Optional[str]) -> dict[str, bool]:
    """Translate Meal.filter_string atoms into template-friendly diet slugs.

    The Django field is free-text with casing variants ('Pork_Free'),
    extra-whitespace runs ('gluten_free  pork_free'), typos ('lactos_free',
    'factose_free'), and French/English mixing ('vegetarien', 'végétarien').
    We lowercase + whitespace-split and look each token up in _TOKEN_TO_SLUG;
    unknown tokens are silently ignored so non-diet flags like 'paleo',
    'first_pos', 'cold_meal' don't pollute the result.

    Returns a dict keyed by CSS slug, in canonical badge order, True when the
    corresponding filter atom is present in any recognized form.
    """
    fs = (filter_string or "").lower()
    found = {
        _TOKEN_TO_SLUG[tok]
        for tok in fs.split()
        if tok in _TOKEN_TO_SLUG
    }
    return {slug: (slug in found) for slug in _DIET_SLUGS}


def diet_labels_fr() -> dict[str, str]:
    """Return the French label dict used when rendering diet badges."""
    return dict(_DIET_LABELS_FR)


def _is_active_today(active_on, inactive_on) -> bool:
    """Canonical 'active today' definition, mirroring Django's Meal.active_at
    (menu/models.py:443) with both bounds inclusive (__lte / __gte).

      active = active_on <= today AND inactive_on >= today

    NULL on either field means the meal is NOT active — Django's __lte / __gte
    filters exclude NULL rows, and unscheduled meals shouldn't auto-publish.
    """
    if active_on is None or inactive_on is None:
        return False
    today = date.today()
    return active_on <= today <= inactive_on


def _format_price(value) -> Optional[str]:
    """Format a Decimal/None price as Shopify expects ('29.99'). Returns None
    when the value is missing so callers can skip price application."""
    if value is None:
        return None
    return f"{float(value):.2f}"


def _plan_label(name: Optional[str]) -> str:
    """Map a `menu_plan.name` to the Shopify variant value (option1).
    Known aliases collapse to canonical labels; unknown names are title-cased."""
    if not name:
        return ""
    cleaned = name.strip()
    return _PLAN_LABEL_ALIASES.get(cleaned.lower(), cleaned.title())


def _build_variants(
    *,
    meal_id: int,
    category: Optional[str],
    unit_price,
    extra_price,
    plans: list[dict],
) -> list[dict]:
    """Build the Shopify variant payload list for one meal.

    For [[_SIZED_CATEGORIES]] (MAIN_DISH, BREAKFAST), emits one variant per
    plan row. Per-variant price = additional_meal_price + unit_price +
    extra_price. Each variant carries option1=<plan label> and a deterministic
    SKU `fresheo-{meal_id}-{plan_id}` so admin filters resolve to the same
    SKU on every re-sync, even if a plan is renamed.

    For other categories, emits a single variant priced from unit_price +
    extra_price with no option1 and SKU `fresheo-{meal_id}`.
    """
    base_extras = float(unit_price or 0) + float(extra_price or 0)

    # Stock tracking is deliberately NOT set on these REST variant payloads.
    # Shopify removed the `inventory_management` field from the REST Admin API
    # in version 2024-04, so sending it is a silent no-op on the version this
    # store runs — the variant just inherits the store-level default (usually
    # tracked) and surfaces a false out-of-stock state. Meals are made-to-order:
    # MealsResource disables tracking per variant via a GraphQL
    # productVariantsBulkUpdate (inventoryItem.tracked=false) right after the
    # product is created or its variants are replaced.
    if category in _SIZED_CATEGORIES and plans:
        variants: list[dict] = []
        for plan in plans:
            label = plan["plan_label"]
            price = float(plan["additional_meal_price"] or 0) + base_extras
            variants.append({
                "price": _format_price(price),
                "option1": label,
                "sku": f"fresheo-{meal_id}-{plan['plan_id']}",
            })
        return variants

    return [{
        "price": _format_price(base_extras),
        "sku": f"fresheo-{meal_id}",
    }]


def _percent_encode_url(url: str) -> str:
    """Percent-encode the path of a URL so spaces and non-ASCII characters are
    valid. Idempotent — already-encoded URLs survive unchanged because we
    decode before re-encoding."""
    parts = urlsplit(url)
    # Decode first to normalize, then re-encode. safe="/" keeps path separators.
    encoded_path = quote(unquote(parts.path), safe="/")
    return urlunsplit(
        (parts.scheme, parts.netloc, encoded_path, parts.query, parts.fragment)
    )


def _resolve_image_urls(row: dict, media_url: str) -> list[str]:
    """Pick the best image URL(s) for a meal.

    Priority:
      1. `meal_image` (full URL, already public)
      2. `picture` joined with DJANGO_MEDIA_URL (if both are present)

    The returned URLs are percent-encoded so Shopify accepts them — raw spaces
    and non-ASCII characters from S3 object keys (e.g. "Riz cantonais.jpeg")
    would otherwise be rejected with 'Image URL is invalid'.

    Returns an empty list if neither source yields a usable URL.
    """
    meal_image = (row.get("meal_image") or "").strip()
    if meal_image:
        return [_percent_encode_url(meal_image)]

    picture_path = (row.get("picture_path") or "").strip()
    if picture_path and media_url:
        return [_percent_encode_url(urljoin(media_url, picture_path.lstrip("/")))]

    return []


async def fetch_meals(
    dsn: str,
    *,
    locale: str = "fr",
    media_url: str = "",
) -> list[dict]:
    """Fetch all customer-visible meals from the Fresheo Django Postgres DB.

    Returns a list of dicts shaped for template rendering and Shopify upload.
    """
    logger.info("[django_db] connecting to Fresheo DB")
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_PLANS_SQL)
            plan_rows = await cur.fetchall()
            await cur.execute(_MEALS_SQL)
            rows = await cur.fetchall()

    plans = [
        {
            "plan_id":               p["plan_id"],
            "plan_label":            _plan_label(p["plan_name"]),
            "additional_meal_price": p["additional_meal_price"],
        }
        for p in plan_rows
    ]
    size_option_values = [p["plan_label"] for p in plans]

    logger.info(
        f"[django_db] fetched {len(rows)} meals; "
        f"{len(plans)} plans for variant pricing: {size_option_values}"
    )

    meals: list[dict] = []
    for row in rows:
        diets = _parse_diet_flags(row["filter_string"])
        nutri_score = (row["nutri_score"] or "A").upper()
        category = row.get("category")
        is_active_today = _is_active_today(
            row.get("active_on"), row.get("inactive_on")
        )
        variants = _build_variants(
            meal_id=row["meal_id"],
            category=category,
            unit_price=row.get("unit_price"),
            extra_price=row.get("extra_price"),
            plans=plans,
        )
        options = (
            [{"name": "Size", "values": size_option_values}]
            if category in _SIZED_CATEGORIES and plans
            else []
        )
        meals.append({
            "meal_id":       row["meal_id"],
            "name":          _extract_locale(row["name_raw"], locale),
            "ingredients":   _extract_locale(row["ingredients_raw"], locale),
            "allergens":     _extract_locale(row["allergens_raw"], locale),
            "nutri_score":   nutri_score,
            "diets":         diets,
            "diet_labels":   _DIET_LABELS_FR,
            "weight":        row["weight"] or 0,
            "kilo_calories": row["kilo_calories"] or 0,
            "proteins":      row["proteins"] or 0,
            "carbohydrates": row["carbohydrates"] or 0,
            "lipids":        row["lipids"] or 0,
            "sugars":        row["sugars"] or 0,
            "saturated":     row["saturated"] or 0,
            "fibers":        row["fibers"] or 0,
            "salts":         row["salts"] or 0,
            "avg_rating":    float(row["avg_rating"] or 5.0),
            "rating_count":  int(row["rating_count"] or 0),
            "image_urls":    _resolve_image_urls(row, media_url),
            # Default-variant price for the rendered description's price block.
            "unit_price":    variants[0]["price"],
            "category":      category,
            "category_label": category_label_fr(category),
            "category_gid":  category_taxonomy_gid(category),
            "is_active_today": is_active_today,
            "tags": _build_tags(
                diets=diets,
                is_active_today=is_active_today,
                category=category,
                nutri_score=nutri_score,
            ),
            "variants": variants,
            "options":  options,
        })

    return meals

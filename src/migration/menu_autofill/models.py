from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

# A meal's "category" (the swap key) is encoded as a *bare* Shopify product tag
# — there is no prefix in this store. The tag is the Django `Meal.category` enum
# kebab-cased, written by `django_db._build_tags` (`MAIN_DISH` -> `main-dish`).
# We therefore recover a meal's category by intersecting its tags with this
# closed vocabulary. Keep in sync with `django_db._CATEGORY_LABELS_FR` keys.
CATEGORY_TAGS: frozenset[str] = frozenset(
    {
        "main-dish",
        "breakfast",
        "dessert",
        "drinks",
        "snack",
        "vacuum",
        "non-food",
    }
)


def category_from_tags(tags: list[str]) -> Optional[str]:
    """Return the single category tag among ``tags``, or None if absent.

    Tags are a flat mix of diet slugs, ``current-menu``, ``nutri-*``,
    ``menu-*`` and the category. Only one category value is expected per
    product; if several are present we return the first by stable sort so the
    choice is deterministic across runs.
    """
    matches = sorted(t for t in tags if t in CATEGORY_TAGS)
    return matches[0] if matches else None


class Decision(str, Enum):
    """Outcome for one subscription's next-order bundle."""

    SKIP = "skip"          # every meal already in the current menu
    ADAPT = "adapt"        # all stale meals swapped same-category; bundle rewritten
    FLAG = "flag"          # a stale meal had no same-category replacement -> manual review, no write
    LOCKED = "locked"      # inside min_lead_hours of the anchor -> never edited
    NO_BUNDLE = "no_bundle"  # subscription is not a BYOB bundle (no bundleTransactionId)
    ERROR = "error"        # read/write/verify failed -> dead-letter


@dataclass(frozen=True)
class MenuMeal:
    """An active-menu meal: a candidate replacement this week."""

    product_id: int
    variant_id: int
    title: str
    category: Optional[str]
    in_stock: bool


@dataclass(frozen=True)
class BundleMeal:
    """A meal in a subscription's effective next-order bundle.

    ``category`` is resolved upstream (from the active menu when the variant is
    in it, otherwise by reading the product's Shopify tags); it may be None when
    the product is gone and no last-known category exists.
    """

    variant_id: int
    quantity: int
    product_id: Optional[int] = None
    title: str = ""
    category: Optional[str] = None


@dataclass
class Swap:
    """One stale meal and its same-category replacement (or lack thereof)."""

    category: Optional[str]
    quantity: int
    removed_variant_id: int
    removed_title: str = ""
    added_variant_id: Optional[int] = None
    added_title: str = ""

    @property
    def resolved(self) -> bool:
        return self.added_variant_id is not None


@dataclass
class PlanResult:
    """The pure planner's verdict for a single bundle.

    ``new_items`` is the full replacement item list for the bundle update API
    (``[{"productVariantShopifyId": int, "quantity": int}, ...]``). It is only
    meaningful when ``decision is Decision.ADAPT`` — on FLAG/SKIP no write
    happens.
    """

    decision: Decision
    new_items: list[dict] = field(default_factory=list)
    swaps: list[Swap] = field(default_factory=list)
    unswappable: list[BundleMeal] = field(default_factory=list)
    kept_valid: list[int] = field(default_factory=list)

    @property
    def removed(self) -> list[int]:
        return [s.removed_variant_id for s in self.swaps if s.resolved]

    @property
    def added(self) -> list[int]:
        return [s.added_variant_id for s in self.swaps if s.resolved]

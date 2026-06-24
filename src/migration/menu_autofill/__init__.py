"""Weekly menu auto-fill for Loop bundle subscriptions.

Each week the active Fresheo menu rotates. Subscribers who don't log in to
re-pick their meals would otherwise have stale (out-of-menu) dishes in their
next box. This package runs daily and, for every subscription whose next
upcoming order still contains meals that are no longer in the current menu,
swaps each stale meal for an active meal of the *same category and same
quantity* via the Loop Storefront Bundle API.

See ``.claude/plans/260623_loop-weekly-menu-autofill-spec.md`` for the full
specification. The decision logic (skip / adapt / flag) lives in
[[planner]] as a pure function; all I/O is wired in [[autofill]].
"""

from .models import (
    CATEGORY_TAGS,
    BundleMeal,
    Decision,
    MenuMeal,
    PlanResult,
    Swap,
)
from .menu import ActiveMenu, build_active_menu
from .planner import plan_bundle

__all__ = [
    "CATEGORY_TAGS",
    "BundleMeal",
    "Decision",
    "MenuMeal",
    "PlanResult",
    "Swap",
    "ActiveMenu",
    "build_active_menu",
    "plan_bundle",
]

from __future__ import annotations

import csv
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..client import ShopifyClient
from .loop_client import LoopAdminClient, LoopStorefrontClient
from .menu import ActiveMenu, build_active_menu, fetch_product_category
from .models import BundleMeal, Decision, PlanResult
from .planner import plan_bundle

logger = logging.getLogger(__name__)

SECONDS_PER_HOUR = 3600

_AUDIT_FIELDS = [
    "subscription_id",
    "customer_shopify_id",
    "billing_date_epoch",
    "hours_to_anchor",
    "decision",
    "removed",
    "added",
    "category_mapping",
    "used_fallback",
    "unswappable",
    "dry_run",
    "ts",
    "ok",
    "error",
]


@dataclass
class AuditRow:
    subscription_id: str = ""
    customer_shopify_id: str = ""
    billing_date_epoch: int = 0
    hours_to_anchor: float = 0.0
    decision: str = ""
    removed: list = field(default_factory=list)
    added: list = field(default_factory=list)
    category_mapping: dict = field(default_factory=dict)
    used_fallback: bool = False
    unswappable: list = field(default_factory=list)
    dry_run: bool = True
    ts: str = ""
    ok: bool = True
    error: str = ""

    def as_csv(self) -> dict:
        return {
            "subscription_id": self.subscription_id,
            "customer_shopify_id": self.customer_shopify_id,
            "billing_date_epoch": self.billing_date_epoch,
            "hours_to_anchor": f"{self.hours_to_anchor:.1f}",
            "decision": self.decision,
            "removed": ";".join(str(v) for v in self.removed),
            "added": ";".join(str(v) for v in self.added),
            "category_mapping": ";".join(f"{k}->{v}" for k, v in self.category_mapping.items()),
            "used_fallback": self.used_fallback,
            "unswappable": ";".join(str(v) for v in self.unswappable),
            "dry_run": self.dry_run,
            "ts": self.ts,
            "ok": self.ok,
            "error": self.error,
        }


def _as_int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        # Tolerate gid strings and numeric strings.
        if isinstance(value, str) and "/" in value:
            value = value.rsplit("/", 1)[-1]
        return int(value)
    except (TypeError, ValueError):
        return None


def _first(d: dict, *keys: str):
    for key in keys:
        if key in d and d[key] is not None:
            return d[key]
    return None


def _subscription_lines(detail: dict) -> list[dict]:
    return (
        _first(detail, "lineItems", "items", "lines", "products")
        or []
    )


def _bundle_transaction_id(detail: dict, lines: list[dict]) -> Optional[str]:
    top = _first(detail, "bundleTransactionId", "_bundleId")
    if top is not None:
        return str(top)
    for line in lines:
        tx = _first(line, "bundleTransactionId", "_bundleId")
        if tx is not None:
            return str(tx)
    return None


class MenuAutofiller:
    """Orchestrates the daily weekly-menu auto-fill (spec Steps 0-6).

    Read path (Steps 0-4) uses only the Shopify Admin API and the Loop **Admin**
    API, so a ``dry_run`` produces a full per-subscription report without minting
    storefront tokens or touching the beta write endpoint. The write path
    (Step 5) and post-write verification (Step 6) run only when
    ``dry_run`` is False and a bundle needs adapting.
    """

    def __init__(
        self,
        *,
        shopify: ShopifyClient,
        admin: LoopAdminClient,
        storefront: LoopStorefrontClient,
        active_menu_tag: str = "current-menu",
        target_lead_hours: int = 48,
        min_lead_hours: int = 24,
        dry_run: bool = True,
        now_epoch: Optional[int] = None,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.shopify = shopify
        self.admin = admin
        self.storefront = storefront
        self.active_menu_tag = active_menu_tag
        self.target_lead_hours = target_lead_hours
        self.min_lead_hours = min_lead_hours
        self.dry_run = dry_run
        self._now_epoch = now_epoch
        self.rng = rng or random.Random()
        self._category_cache: dict[int, Optional[str]] = {}

    def _now(self) -> int:
        if self._now_epoch is not None:
            return self._now_epoch
        return int(datetime.now(timezone.utc).timestamp())

    def _ts(self) -> str:
        return datetime.fromtimestamp(self._now(), tz=timezone.utc).isoformat()

    async def run(self, *, limit: Optional[int] = None) -> list[AuditRow]:
        """Execute the full pass and return one audit row per subscription seen."""
        now = self._now()
        menu = await build_active_menu(self.shopify, active_menu_tag=self.active_menu_tag)
        if not menu.active_variant_ids:
            # Refuse to act on an empty menu: every bundle would look stale and we
            # could blow away every subscriber's box. Treat as a hard stop.
            raise RuntimeError(
                f"Active menu is empty (no in-stock variants tagged "
                f"{self.active_menu_tag!r}); aborting to avoid mass-adapting bundles."
            )

        start = now
        end = now + self.target_lead_hours * SECONDS_PER_HOUR
        rows: list[AuditRow] = []
        async for sched in self.admin.iter_scheduled_orders(
            billing_start_epoch=start, billing_end_epoch=end
        ):
            row = await self._process_one(sched, menu)
            rows.append(row)
            if limit is not None and len(rows) >= limit:
                logger.info("[autofill] reached limit=%d, stopping discovery", limit)
                break

        self._log_summary(rows)
        return rows

    async def _process_one(self, sched: dict, menu: ActiveMenu) -> AuditRow:
        now = self._now()
        sub = sched.get("subscription") or {}
        customer = sched.get("customer") or {}
        subscription_id = _first(sub, "id") or _first(sched, "subscriptionId", "id")
        customer_id = _first(customer, "shopifyId", "id") or _first(sched, "customerShopifyId")
        billing_epoch = _as_int(_first(sched, "billingDateEpoch", "billingDate")) or 0

        row = AuditRow(
            subscription_id=str(subscription_id) if subscription_id is not None else "",
            customer_shopify_id=str(customer_id) if customer_id is not None else "",
            billing_date_epoch=billing_epoch,
            hours_to_anchor=(billing_epoch - now) / SECONDS_PER_HOUR if billing_epoch else 0.0,
            dry_run=self.dry_run,
            ts=self._ts(),
        )

        try:
            # Step 1 cutoff guard: never edit inside the lock window.
            if billing_epoch and row.hours_to_anchor < self.min_lead_hours:
                row.decision = Decision.LOCKED.value
                logger.info(
                    "[autofill] sub %s LOCKED (%.1fh < %dh lead)",
                    row.subscription_id, row.hours_to_anchor, self.min_lead_hours,
                )
                return row

            if subscription_id is None:
                raise RuntimeError(f"scheduled order missing subscription id: {sched}")

            # Step 2: read subscription contents.
            detail = await self.admin.get_subscription(subscription_id)
            lines = _subscription_lines(detail)
            tx_id = _bundle_transaction_id(detail, lines)
            if not tx_id:
                row.decision = Decision.NO_BUNDLE.value
                logger.info("[autofill] sub %s has no bundleTransactionId, skipping", row.subscription_id)
                return row

            effective = await self._effective_meals(lines, menu)

            # Steps 3-4: decide and build the new bundle.
            plan = plan_bundle(effective, menu, rng=self.rng)
            self._fill_row_from_plan(row, plan)

            if plan.decision is Decision.SKIP:
                return row

            if plan.decision is Decision.FLAG:
                row.ok = False
                row.error = "no same-category replacement for: " + ", ".join(
                    str(m.variant_id) for m in plan.unswappable
                )
                logger.warning("[autofill] sub %s FLAGGED for review — %s", row.subscription_id, row.error)
                return row

            # decision is ADAPT
            if self.dry_run:
                logger.info(
                    "[autofill] [DRY RUN] sub %s would adapt: %s -> %s",
                    row.subscription_id, plan.removed, plan.added,
                )
                return row

            await self._write_and_verify(row, plan, tx_id, customer_id, menu)
            return row

        except Exception as exc:  # noqa: BLE001 — one failure must not abort the batch
            row.decision = row.decision or Decision.ERROR.value
            row.ok = False
            row.error = str(exc)
            logger.error("[autofill] sub %s FAILED — %s", row.subscription_id, exc)
            return row

    async def _effective_meals(self, lines: list[dict], menu: ActiveMenu) -> list[BundleMeal]:
        """Build the effective next-order meals from subscription lines.

        Folds in one-time next-order edits: lines flagged ``isOneTimeRemoved``
        are dropped, ``isOneTimeAdded`` are kept, so we judge what actually ships.
        Each meal's category is resolved from the active menu when present, else
        by reading the product's Shopify tags (cached).
        """
        meals: list[BundleMeal] = []
        for line in lines:
            if bool(_first(line, "isOneTimeRemoved")):
                continue
            variant_id = _as_int(_first(line, "variantShopifyId", "productVariantShopifyId", "variantId"))
            if variant_id is None:
                continue
            product_id = _as_int(_first(line, "productShopifyId", "productId"))
            quantity = _as_int(_first(line, "quantity")) or 1
            title = _first(line, "title", "productTitle", "name") or ""
            category = await self._resolve_category(variant_id, product_id, menu)
            meals.append(
                BundleMeal(
                    variant_id=variant_id,
                    quantity=quantity,
                    product_id=product_id,
                    title=str(title),
                    category=category,
                )
            )
        return meals

    async def _resolve_category(
        self, variant_id: int, product_id: Optional[int], menu: ActiveMenu
    ) -> Optional[str]:
        # Active-menu variants carry their category already.
        if variant_id in menu.variant_to_category:
            return menu.variant_to_category[variant_id]
        if product_id is None:
            return None
        if product_id in self._category_cache:
            return self._category_cache[product_id]
        category = await fetch_product_category(self.shopify, product_id)
        self._category_cache[product_id] = category
        return category

    def _fill_row_from_plan(self, row: AuditRow, plan: PlanResult) -> None:
        row.decision = plan.decision.value
        row.removed = plan.removed
        row.added = plan.added
        row.unswappable = [m.variant_id for m in plan.unswappable]
        row.category_mapping = {
            str(s.removed_variant_id): (s.category or "?") for s in plan.swaps
        }
        row.used_fallback = False  # default-category fallback is disabled by design

    async def _write_and_verify(
        self,
        row: AuditRow,
        plan: PlanResult,
        tx_id: str,
        customer_id,
        menu: ActiveMenu,
    ) -> None:
        # Read box size / discount immediately before the write (Step 5).
        bundle_tx = await self.storefront.get_bundle_transaction(customer_id, tx_id)
        box_size_id = _first(bundle_tx, "boxSizeId", "boxsizeId")
        discount_id = _first(bundle_tx, "discountId")

        await self.storefront.update_bundle(
            customer_id,
            transaction_id=tx_id,
            items=plan.new_items,
            discount_id=discount_id,
            box_size_id=box_size_id,
        )
        logger.info("[autofill] sub %s adapted: %s -> %s", row.subscription_id, plan.removed, plan.added)

        # Step 6: re-read and assert the upcoming order is now all in-menu.
        detail = await self.admin.get_subscription(row.subscription_id)
        lines = _subscription_lines(detail)
        post = await self._effective_meals(lines, menu)
        still_stale = [m.variant_id for m in post if m.variant_id not in menu.active_variant_ids]
        if still_stale:
            row.ok = False
            row.error = f"post-write verification failed; still stale: {still_stale}"
            logger.error("[autofill] sub %s VERIFY FAILED — %s", row.subscription_id, row.error)

    def _log_summary(self, rows: list[AuditRow]) -> None:
        counts: dict[str, int] = {}
        for r in rows:
            counts[r.decision or "?"] = counts.get(r.decision or "?", 0) + 1
        failed = sum(1 for r in rows if not r.ok)
        logger.info("[autofill] done — %d subscriptions, decisions=%s, failures=%d", len(rows), counts, failed)


def write_audit(rows: list[AuditRow], path: Path) -> None:
    """Write the per-subscription audit log to CSV."""
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_AUDIT_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.as_csv())

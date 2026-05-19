from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import httpx

from .base import BaseResource, atomic_write_json
from ..client import ShopifyClient
from ..id_map import IDMap
from ..logger import FailedResourcesLog
from ..progress import ProgressTracker

logger = logging.getLogger(__name__)


class InventoryResource(BaseResource):
    """Migrates inventory levels per location."""

    resource_name = "inventory"
    endpoint = "inventory_levels.json"
    resource_key = "inventory_level"
    list_key = "inventory_levels"

    def __init__(
        self,
        *args,
        products_id_map: IDMap,
        location_map_override: dict | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._products_id_map = products_id_map
        self._location_map_override = location_map_override or {}
        self._location_map: dict[str, str] = {}

    async def _fetch_locations(self, client: ShopifyClient) -> list[dict]:
        try:
            response = await client.get("locations.json")
            return response.get("locations", [])
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 403:
                logger.warning(
                    "[inventory] 403 on locations.json — the app is missing 'read_locations' "
                    "scope or needs to be reinstalled on the store to activate updated scopes. "
                    "Skipping inventory."
                )
                return []
            raise

    async def _build_location_map(self) -> None:
        """Match source locations to dest locations by name."""
        source_locs = await self._fetch_locations(self.source)
        dest_locs = await self._fetch_locations(self.dest)

        dest_by_name = {loc["name"]: str(loc["id"]) for loc in dest_locs}

        for loc in source_locs:
            src_id = str(loc["id"])
            name = loc.get("name", "")

            # Check manual override first
            if src_id in self._location_map_override:
                self._location_map[src_id] = str(self._location_map_override[src_id])
            elif name in dest_by_name:
                self._location_map[src_id] = dest_by_name[name]
            else:
                logger.warning(
                    f"[load] inventory: no dest location match for '{name}' (id={src_id}). "
                    f"Provide a location_map.json override to resolve."
                )

        logger.info(
            f"[load] inventory: mapped {len(self._location_map)}/{len(source_locs)} locations"
        )

    async def _fetch_all(self) -> list[dict]:
        # Fetch all locations first to paginate inventory per location
        source_locs = await self._fetch_locations(self.source)
        if not source_locs:
            return []

        location_ids = ",".join(str(loc["id"]) for loc in source_locs)
        all_levels: list[dict] = []
        async for page in self.source.get_paginated(
            "inventory_levels.json",
            "inventory_levels",
            params={"location_ids": location_ids},
        ):
            all_levels.extend(page)
        return all_levels

    def transform(self, item: dict) -> dict:
        # Not used directly — load is fully custom
        return item

    async def load(self, force: bool = False) -> None:
        if not self._data_file.exists():
            logger.warning("[load] inventory: data file not found, skipping")
            return

        await self._build_location_map()

        # Build inventory_item_id mapping: need to look up dest variant for each source variant
        # inventory_item_id is stored on the variant; we need source_variant → dest_inventory_item
        await self._build_inventory_item_map()

        levels: list[dict] = json.loads(self._data_file.read_text(encoding="utf-8"))
        logger.info(f"[load] inventory: starting ({len(levels)} levels)")
        updated = skipped = failed = 0

        for level in levels:
            src_location_id = str(level.get("location_id", ""))
            src_item_id = str(level.get("inventory_item_id", ""))
            available = level.get("available", 0) or 0

            dest_location_id = self._location_map.get(src_location_id)
            dest_item_id = self._inventory_item_map.get(src_item_id)

            if not dest_location_id or not dest_item_id:
                skipped += 1
                continue

            if self.dry_run:
                logger.info(
                    f"[DRY RUN] would set inventory: item={dest_item_id} "
                    f"loc={dest_location_id} available={available}"
                )
                continue

            try:
                await self.dest.post(
                    "inventory_levels/set.json",
                    {
                        "location_id": dest_location_id,
                        "inventory_item_id": dest_item_id,
                        "available": available,
                    },
                )
                updated += 1
            except Exception as exc:
                logger.error(f"[load] inventory: FAILED for item={src_item_id} — {exc}")
                failed += 1

        self.progress.mark_resource_done(self.resource_name)
        logger.info(
            f"[load] inventory: done (updated={updated}, skipped={skipped}, failed={failed})"
        )

    async def _build_inventory_item_map(self) -> None:
        """
        Map source inventory_item_id → dest inventory_item_id.
        Strategy: for each dest product (from products ID map), fetch its variants
        and record the inventory_item_id mapping.
        """
        self._inventory_item_map: dict[str, str] = {}

        # Read source products to get source variant → source inventory_item_id
        source_products_file = self.data_dir / "products.json"
        if not source_products_file.exists():
            logger.warning("[load] inventory: products.json not found, cannot map inventory items")
            return

        source_products: list[dict] = json.loads(
            source_products_file.read_text(encoding="utf-8")
        )

        # Build source variant_id → inventory_item_id
        src_variant_to_item: dict[str, str] = {}
        for product in source_products:
            for variant in product.get("variants", []):
                vid = str(variant.get("id", ""))
                iid = str(variant.get("inventory_item_id", ""))
                if vid and iid:
                    src_variant_to_item[vid] = iid

        # For each source product, look up dest product variants
        for product in source_products:
            src_product_id = str(product.get("id", ""))
            dest_product_id = self._products_id_map.get(src_product_id)
            if not dest_product_id:
                continue

            try:
                response = await self.dest.get(f"products/{dest_product_id}/variants.json")
                dest_variants = response.get("variants", [])
                src_variants = product.get("variants", [])

                # Match by position (order) — variants are created in same order
                for i, dest_variant in enumerate(dest_variants):
                    if i < len(src_variants):
                        src_iid = src_variant_to_item.get(str(src_variants[i].get("id", "")))
                        dest_iid = str(dest_variant.get("inventory_item_id", ""))
                        if src_iid and dest_iid:
                            self._inventory_item_map[src_iid] = dest_iid
            except Exception as exc:
                logger.warning(
                    f"[load] inventory: could not fetch dest variants for "
                    f"product {dest_product_id}: {exc}"
                )

        logger.info(
            f"[load] inventory: mapped {len(self._inventory_item_map)} inventory items"
        )

    async def find_existing(self, item: dict) -> Optional[dict]:
        return None  # Inventory uses set.json, not create

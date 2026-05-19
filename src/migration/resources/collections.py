from __future__ import annotations

import json
import logging
from typing import Optional

from .base import BaseResource, _BASE_STRIP_FIELDS, atomic_write_json
from ..id_map import IDMap

logger = logging.getLogger(__name__)

_COLLECTION_STRIP = _BASE_STRIP_FIELDS | {"published_at"}


class CollectionsResource(BaseResource):
    """Migrates both custom (manual) and smart collections, plus manual memberships."""

    resource_name = "collections"
    endpoint = "custom_collections.json"
    resource_key = "custom_collection"
    list_key = "custom_collections"

    def __init__(self, *args, products_id_map: IDMap, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._products_id_map = products_id_map

    async def _fetch_all(self) -> list[dict]:
        all_items: list[dict] = []

        async for page in self.source.get_paginated("custom_collections.json", "custom_collections"):
            for c in page:
                c["_type"] = "custom"
            all_items.extend(page)

        async for page in self.source.get_paginated("smart_collections.json", "smart_collections"):
            for c in page:
                c["_type"] = "smart"
            all_items.extend(page)

        return all_items

    async def extract(self, force: bool = False) -> list[dict]:
        items = await super().extract(force)

        # Also extract collection memberships (collects)
        collects_file = self.data_dir / "collection_memberships.json"
        if force or not collects_file.exists():
            logger.info("[extract] collection_memberships: starting")
            all_collects: list[dict] = []
            async for page in self.source.get_paginated("collects.json", "collects"):
                all_collects.extend(page)
            atomic_write_json(collects_file, all_collects)
            logger.info(f"[extract] collection_memberships: done ({len(all_collects)} items)")

        return items

    def transform(self, item: dict) -> dict:
        # _type is kept here so _create can pop it to choose the correct endpoint
        return {k: v for k, v in item.items() if k not in _COLLECTION_STRIP}

    async def find_existing(self, item: dict) -> Optional[dict]:
        handle = item.get("handle")
        if not handle:
            return None
        ctype = item.get("_type", "custom")
        if ctype == "custom":
            response = await self.dest.get(
                "custom_collections.json", params={"handle": handle}
            )
            results = response.get("custom_collections", [])
        else:
            response = await self.dest.get(
                "smart_collections.json", params={"handle": handle}
            )
            results = response.get("smart_collections", [])
        return results[0] if results else None

    async def _create(self, payload: dict) -> str:
        ctype = payload.pop("_type", "custom")
        if ctype == "custom":
            response = await self.dest.post(
                "custom_collections.json", {"custom_collection": payload}
            )
            return str(response["custom_collection"]["id"])
        else:
            response = await self.dest.post(
                "smart_collections.json", {"smart_collection": payload}
            )
            return str(response["smart_collection"]["id"])

    async def load_memberships(self, force: bool = False) -> None:
        """Load manual collection memberships (collects) after products and collections are loaded."""
        collects_file = self.data_dir / "collection_memberships.json"
        if not collects_file.exists():
            logger.warning("[load] collection_memberships: file not found, skipping")
            return

        collects: list[dict] = json.loads(collects_file.read_text(encoding="utf-8"))
        logger.info(f"[load] collection_memberships: starting ({len(collects)} items)")
        created = 0
        skipped = 0
        failed = 0

        for collect in collects:
            source_product_id = str(collect.get("product_id", ""))
            source_collection_id = str(collect.get("collection_id", ""))

            dest_product_id = self._products_id_map.get(source_product_id)
            dest_collection_id = self.id_map.get(source_collection_id)

            if not dest_product_id or not dest_collection_id:
                logger.warning(
                    f"[load] collect: skipping — product_id={source_product_id} "
                    f"or collection_id={source_collection_id} not in ID map"
                )
                skipped += 1
                continue

            if self.dry_run:
                logger.info(
                    f"[DRY RUN] would create collect: "
                    f"product={dest_product_id} collection={dest_collection_id}"
                )
                continue

            try:
                await self.dest.post(
                    "collects.json",
                    {"collect": {"product_id": dest_product_id, "collection_id": dest_collection_id}},
                )
                created += 1
            except Exception as exc:
                # 422 "already exists" is acceptable
                if "already exists" in str(exc).lower():
                    skipped += 1
                else:
                    logger.error(f"[load] collect: FAILED — {exc}")
                    failed += 1

        logger.info(
            f"[load] collection_memberships: done "
            f"(created={created}, skipped={skipped}, failed={failed})"
        )

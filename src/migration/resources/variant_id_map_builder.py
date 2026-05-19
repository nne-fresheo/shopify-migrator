from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

from ..client import ShopifyClient

logger = logging.getLogger(__name__)


async def _fetch_sku_to_variant_id(client: ShopifyClient, store_label: str) -> dict[str, str]:
    """
    Fetch all products from a store and return {sku: variant_id}.
    SKUs that appear more than once are excluded with a warning.
    """
    sku_map: dict[str, str] = {}
    duplicate_skus: set[str] = set()

    async for page in client.get_paginated(
        "products.json", "products", params={"status": "any"}
    ):
        for product in page:
            for variant in product.get("variants", []):
                sku = (variant.get("sku") or "").strip()
                if not sku:
                    continue  # Skip blank SKUs — unresolvable

                variant_id = str(variant["id"])

                if sku in duplicate_skus:
                    continue  # Already flagged, skip silently
                if sku in sku_map:
                    logger.warning(
                        f"[variant_id_map] {store_label}: duplicate SKU '{sku}' "
                        f"(variant {sku_map[sku]} and {variant_id}) — skipping both"
                    )
                    del sku_map[sku]
                    duplicate_skus.add(sku)
                    continue

                sku_map[sku] = variant_id

    logger.info(
        f"[variant_id_map] {store_label}: {len(sku_map)} unique SKUs found "
        f"({len(duplicate_skus)} duplicate SKUs excluded)"
    )
    return sku_map


async def build_variant_id_map(
    source: ShopifyClient,
    dest: ShopifyClient,
    cache_path: Optional[Path] = None,
) -> dict[str, str]:
    """
    Build a source_variant_id -> dest_variant_id mapping using SKU matching.

    Fetches all products+variants from both stores concurrently and matches by SKU.
    Variants with blank SKUs or duplicate SKUs within the same store are excluded.

    Parameters
    ----------
    source : ShopifyClient
        Authenticated client for the source store.
    dest : ShopifyClient
        Authenticated client for the destination store.
    cache_path : Path, optional
        If provided and the file exists, loads from it instead of hitting the API.
        If provided and the file does not exist, saves the result atomically after building.

    Returns
    -------
    dict[str, str]
        Maps source variant IDs (as str) to destination variant IDs (as str).
    """
    if cache_path and cache_path.exists():
        try:
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
            result = {str(k): str(v) for k, v in raw.items()}
            logger.info(
                f"[variant_id_map] Loaded {len(result)} mappings from cache: {cache_path}"
            )
            return result
        except Exception as exc:
            logger.warning(
                f"[variant_id_map] Failed to load cache at {cache_path}: {exc} — rebuilding"
            )

    logger.info("[variant_id_map] Fetching variants from source and destination stores...")

    source_sku_map, dest_sku_map = await asyncio.gather(
        _fetch_sku_to_variant_id(source, "source"),
        _fetch_sku_to_variant_id(dest, "dest"),
    )

    result: dict[str, str] = {}
    unmatched = 0

    for sku, src_variant_id in source_sku_map.items():
        dest_variant_id = dest_sku_map.get(sku)
        if dest_variant_id:
            result[src_variant_id] = dest_variant_id
        else:
            unmatched += 1

    logger.info(
        f"[variant_id_map] Built map: {len(result)} matched, "
        f"{unmatched} source SKUs not found on destination"
    )

    if cache_path:
        try:
            tmp = cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(result, indent=2), encoding="utf-8")
            os.replace(tmp, cache_path)
            logger.info(f"[variant_id_map] Saved cache to {cache_path}")
        except Exception as exc:
            logger.warning(f"[variant_id_map] Could not save cache: {exc}")

    return result

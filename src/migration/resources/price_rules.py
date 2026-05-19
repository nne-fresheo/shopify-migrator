from __future__ import annotations

import logging
from typing import Optional

from .base import BaseResource, _BASE_STRIP_FIELDS
from ..id_map import IDMap

logger = logging.getLogger(__name__)

_PRICE_RULE_STRIP = _BASE_STRIP_FIELDS | {"usage_count"}


class PriceRulesResource(BaseResource):
    resource_name = "price_rules"
    endpoint = "price_rules.json"
    resource_key = "price_rule"
    list_key = "price_rules"

    def __init__(
        self,
        *args,
        collections_id_map: IDMap,
        variants_id_map: Optional[IDMap] = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._collections_id_map = collections_id_map
        self._variants_id_map = variants_id_map

    def transform(self, item: dict) -> dict:
        payload = {k: v for k, v in item.items() if k not in _PRICE_RULE_STRIP}
        title = payload.get("title")

        # Remap entitled_collection_ids
        payload["entitled_collection_ids"] = [
            dest_id
            for src_id in payload.get("entitled_collection_ids", [])
            if (dest_id := self._collections_id_map.get(str(src_id)))
        ]

        # Remap prerequisite_collection_ids
        payload["prerequisite_collection_ids"] = [
            dest_id
            for src_id in payload.get("prerequisite_collection_ids", [])
            if (dest_id := self._collections_id_map.get(str(src_id)))
        ]

        # Remap variant IDs via SKU-based map; clear IDs that cannot be mapped.
        for field in ("entitled_variant_ids", "prerequisite_variant_ids"):
            src_ids = payload.get(field) or []
            if not src_ids:
                payload[field] = []
                continue

            if self._variants_id_map is None:
                logger.warning(
                    f"[transform] price_rule '{title}': clearing {field} "
                    f"({len(src_ids)} IDs) — no variants_id_map provided"
                )
                payload[field] = []
                continue

            remapped = []
            for src_id in src_ids:
                dest_id = self._variants_id_map.get(str(src_id))
                if dest_id:
                    remapped.append(dest_id)
                else:
                    logger.warning(
                        f"[transform] price_rule '{title}': "
                        f"variant {src_id} not in variants_id_map (no SKU match) — dropping"
                    )
            payload[field] = remapped

        # Product IDs cannot be mapped (no product ID map in this resource) — clear them.
        for field in ("entitled_product_ids", "prerequisite_product_ids"):
            if payload.get(field):
                logger.warning(
                    f"[transform] price_rule '{title}': clearing {field} "
                    f"({len(payload[field])} IDs) — product ID mapping not supported"
                )
                payload[field] = []

        # If entitled target_selection now has no entitled items, fall back to "all".
        # Note: Shopify rejects allocation_method="each" + target_selection="all" with no
        # entitled items (422). Change allocation_method to "across" in that case, which
        # Shopify accepts. Log at ERROR level because this changes discount semantics.
        has_entitled = (
            payload.get("entitled_variant_ids")
            or payload.get("entitled_product_ids")
            or payload.get("entitled_collection_ids")
        )
        if payload.get("target_selection") == "entitled" and not has_entitled:
            if payload.get("allocation_method") == "each":
                logger.error(
                    f"[transform] price_rule '{title}': allocation_method='each' with no "
                    "entitled items — Shopify rejects each+all. Changing allocation_method "
                    "to 'across' and target_selection to 'all'. REVIEW THIS RULE MANUALLY."
                )
                payload["allocation_method"] = "across"
            else:
                logger.warning(
                    f"[transform] price_rule '{title}': "
                    "no entitled items after ID mapping — changing target_selection to 'all'"
                )
            payload["target_selection"] = "all"

        return payload

    async def find_existing(self, item: dict) -> Optional[dict]:
        title = item.get("title")
        if not title:
            return None
        response = await self.dest.get("price_rules.json", params={"title": title, "limit": 1})
        rules = response.get("price_rules", [])
        return rules[0] if rules else None

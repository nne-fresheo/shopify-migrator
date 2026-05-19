from __future__ import annotations

import json
import logging
from typing import Optional

from .base import BaseResource, _BASE_STRIP_FIELDS, atomic_write_json
from ..id_map import IDMap

logger = logging.getLogger(__name__)

_CODE_STRIP = _BASE_STRIP_FIELDS | {"price_rule_id", "usage_count", "errors"}


class DiscountCodesResource(BaseResource):
    """Migrates discount codes. Requires price_rules to be loaded first."""

    resource_name = "discount_codes"
    endpoint = "discount_codes.json"
    resource_key = "discount_code"
    list_key = "discount_codes"

    def __init__(self, *args, price_rules_id_map: IDMap, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._price_rules_id_map = price_rules_id_map

    async def _fetch_all(self) -> list[dict]:
        # Discount codes must be fetched per price rule
        all_codes: list[dict] = []
        async for page in self.source.get_paginated("price_rules.json", "price_rules"):
            for rule in page:
                rule_id = rule["id"]
                async for code_page in self.source.get_paginated(
                    f"price_rules/{rule_id}/discount_codes.json", "discount_codes"
                ):
                    for code in code_page:
                        code["_source_price_rule_id"] = str(rule_id)
                    all_codes.extend(code_page)

        return all_codes

    def transform(self, item: dict) -> dict:
        # _dest_price_rule_id is kept here so _create can pop it before sending to API
        strip = _CODE_STRIP | {"_source_price_rule_id"}
        return {k: v for k, v in item.items() if k not in strip}

    async def _create(self, payload: dict) -> str:
        dest_rule_id = payload.pop("_dest_price_rule_id", None)
        if not dest_rule_id:
            raise RuntimeError("Missing _dest_price_rule_id — load price_rules first")

        response = await self.dest.post(
            f"price_rules/{dest_rule_id}/discount_codes.json",
            {"discount_code": payload},
        )
        return str(response["discount_code"]["id"])

    async def _load_item(self, item: dict, force: bool = False) -> str | None:
        item = dict(item)
        source_rule_id = item.get("_source_price_rule_id", "")
        dest_rule_id = self._price_rules_id_map.get(source_rule_id)
        if not dest_rule_id:
            logger.warning(
                f"[load] discount_codes: price_rule {source_rule_id} not mapped, "
                f"skipping code '{item.get('code')}'"
            )
            return None
        item["_dest_price_rule_id"] = dest_rule_id
        return await super()._load_item(item, force)

    async def find_existing(self, item: dict) -> Optional[dict]:
        code = item.get("code")
        source_rule_id = item.get("_source_price_rule_id", "")
        dest_rule_id = self._price_rules_id_map.get(source_rule_id)
        if not code or not dest_rule_id:
            return None
        response = await self.dest.get(
            f"price_rules/{dest_rule_id}/discount_codes.json",
            params={"code": code},
        )
        codes = response.get("discount_codes", [])
        return codes[0] if codes else None

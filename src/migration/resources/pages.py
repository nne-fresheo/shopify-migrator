from __future__ import annotations

import logging
from typing import Optional

from .base import BaseResource, _BASE_STRIP_FIELDS

logger = logging.getLogger(__name__)

_PAGE_STRIP = _BASE_STRIP_FIELDS | {"published_at", "shop_id"}


class PagesResource(BaseResource):
    resource_name = "pages"
    endpoint = "pages.json"
    resource_key = "page"
    list_key = "pages"

    def transform(self, item: dict) -> dict:
        return {k: v for k, v in item.items() if k not in _PAGE_STRIP}

    async def find_existing(self, item: dict) -> Optional[dict]:
        handle = item.get("handle")
        if not handle:
            return None
        response = await self.dest.get("pages.json", params={"handle": handle})
        pages = response.get("pages", [])
        return pages[0] if pages else None

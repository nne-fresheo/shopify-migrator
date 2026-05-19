from __future__ import annotations

import logging
from typing import Optional

from .base import BaseResource, _BASE_STRIP_FIELDS

logger = logging.getLogger(__name__)


class RedirectsResource(BaseResource):
    resource_name = "redirects"
    endpoint = "redirects.json"
    resource_key = "redirect"
    list_key = "redirects"

    def transform(self, item: dict) -> dict:
        return {k: v for k, v in item.items() if k not in _BASE_STRIP_FIELDS}

    async def find_existing(self, item: dict) -> Optional[dict]:
        path = item.get("path")
        if not path:
            return None
        response = await self.dest.get("redirects.json", params={"path": path})
        redirects = response.get("redirects", [])
        return redirects[0] if redirects else None

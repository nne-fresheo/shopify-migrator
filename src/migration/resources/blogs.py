from __future__ import annotations

import logging
from typing import Optional

from .base import BaseResource, _BASE_STRIP_FIELDS

logger = logging.getLogger(__name__)

_BLOG_STRIP = _BASE_STRIP_FIELDS | {"published_at"}


class BlogsResource(BaseResource):
    resource_name = "blogs"
    endpoint = "blogs.json"
    resource_key = "blog"
    list_key = "blogs"

    def transform(self, item: dict) -> dict:
        return {k: v for k, v in item.items() if k not in _BLOG_STRIP}

    async def find_existing(self, item: dict) -> Optional[dict]:
        handle = item.get("handle")
        if not handle:
            return None
        response = await self.dest.get("blogs.json", params={"handle": handle})
        blogs = response.get("blogs", [])
        return blogs[0] if blogs else None

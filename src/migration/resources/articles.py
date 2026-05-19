from __future__ import annotations

import logging
from typing import Optional

from .base import BaseResource, _BASE_STRIP_FIELDS
from ..id_map import IDMap

logger = logging.getLogger(__name__)

_ARTICLE_STRIP = _BASE_STRIP_FIELDS | {"published_at", "user_id", "blog_id"}


class ArticlesResource(BaseResource):
    """Migrates blog articles. Requires blogs to be loaded first."""

    resource_name = "articles"
    endpoint = "articles.json"  # Not used directly — articles are under /blogs/{id}/articles.json
    resource_key = "article"
    list_key = "articles"

    def __init__(self, *args, blogs_id_map: IDMap, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._blogs_id_map = blogs_id_map

    async def _fetch_all(self) -> list[dict]:
        # Articles must be fetched per-blog
        blogs_response = await self.source.get("blogs.json")
        blogs = blogs_response.get("blogs", [])

        all_articles: list[dict] = []
        for blog in blogs:
            blog_id = blog["id"]
            async for page in self.source.get_paginated(
                f"blogs/{blog_id}/articles.json", "articles"
            ):
                for article in page:
                    article["_source_blog_id"] = str(blog_id)
                all_articles.extend(page)

        return all_articles

    async def _create(self, payload: dict) -> str:
        # Resolve destination blog_id
        source_blog_id = payload.pop("_source_blog_id_for_create", None)
        if not source_blog_id:
            raise RuntimeError("Missing _source_blog_id_for_create in article payload")

        dest_blog_id = self._blogs_id_map.get(source_blog_id)
        if not dest_blog_id:
            raise RuntimeError(
                f"Blog ID {source_blog_id} not in blogs ID map — load blogs first"
            )

        response = await self.dest.post(
            f"blogs/{dest_blog_id}/articles.json", {"article": payload}
        )
        return str(response["article"]["id"])

    async def _load_item(self, item: dict, force: bool = False) -> str | None:
        # Stash source blog ID on the item so _create can access it
        item = dict(item)
        item["_source_blog_id_for_create"] = item.get("_source_blog_id", "")
        # Remove from transform payload but keep for _create via the dict copy
        return await super()._load_item(item, force)

    def transform(self, item: dict) -> dict:
        # _source_blog_id_for_create is kept here so _create can pop it before sending to API
        strip = _ARTICLE_STRIP | {"_source_blog_id"}
        return {k: v for k, v in item.items() if k not in strip}

    async def find_existing(self, item: dict) -> Optional[dict]:
        handle = item.get("handle")
        source_blog_id = item.get("_source_blog_id", "")
        dest_blog_id = self._blogs_id_map.get(source_blog_id)
        if not handle or not dest_blog_id:
            return None
        response = await self.dest.get(
            f"blogs/{dest_blog_id}/articles.json", params={"handle": handle}
        )
        articles = response.get("articles", [])
        return articles[0] if articles else None

from __future__ import annotations

import logging
import re
from typing import Optional

from .base import BaseResource
from ..id_map import IDMap

logger = logging.getLogger(__name__)

_GQL_GET_MENUS = """
query getMenus($cursor: String) {
  menus(first: 50, after: $cursor) {
    edges {
      node {
        id
        handle
        title
        items {
          id
          title
          type
          url
          resourceId
          items {
            id
            title
            type
            url
            resourceId
            items {
              id
              title
              type
              url
              resourceId
            }
          }
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_GQL_CREATE_MENU = """
mutation menuCreate($title: String!, $handle: String!, $items: [MenuItemCreateInput!]!) {
  menuCreate(title: $title, handle: $handle, items: $items) {
    menu { id handle title }
    userErrors { field message }
  }
}
"""

_GQL_GET_MENU_BY_HANDLE = """
query getMenuByHandle($handle: String!) {
  menus(first: 1, query: $handle) {
    edges { node { id handle } }
  }
}
"""


class MenusResource(BaseResource):
    """Migrates navigation menus via GraphQL. Must run last — all content must exist."""

    resource_name = "menus"
    endpoint = ""
    resource_key = ""
    list_key = ""

    def __init__(
        self,
        *args,
        products_id_map: IDMap,
        collections_id_map: IDMap,
        pages_id_map: IDMap,
        blogs_id_map: IDMap,
        articles_id_map: IDMap,
        source_domain: str,
        dest_domain: str,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._id_maps = {
            "PRODUCT": products_id_map,
            "COLLECTION": collections_id_map,
            "PAGE": pages_id_map,
            "BLOG": blogs_id_map,
            "ARTICLE": articles_id_map,
        }
        self._source_domain = source_domain
        self._dest_domain = dest_domain

    async def _fetch_all(self) -> list[dict]:
        all_menus: list[dict] = []
        cursor = None

        while True:
            try:
                data = await self.source.graphql(
                    _GQL_GET_MENUS,
                    variables={"cursor": cursor},
                    estimated_cost=100,
                )
            except RuntimeError as exc:
                if "ACCESS_DENIED" in str(exc):
                    logger.warning(
                        "[extract] menus: access denied — add 'read_online_store_navigation' "
                        "scope to the source app and reinstall to migrate menus."
                    )
                    return []
                raise

            edges = data.get("menus", {}).get("edges", [])
            page_info = data.get("menus", {}).get("pageInfo", {})

            for edge in edges:
                all_menus.append(edge["node"])

            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")

        return all_menus

    def transform(self, item: dict) -> dict:
        return item

    def _remap_item(self, item: dict) -> dict:
        """Recursively remap a menu item's resourceId and URL."""
        result: dict = {
            "title": item.get("title"),
            "type": item.get("type", "URL"),
        }

        item_type = item.get("type", "URL").upper()
        resource_gid = item.get("resourceId")
        url = item.get("url", "")

        # Remap resource IDs for typed items
        if resource_gid and item_type in self._id_maps:
            src_id = resource_gid.split("/")[-1]
            id_map = self._id_maps[item_type]
            dest_id = id_map.get(src_id)
            if dest_id:
                resource_type = item_type.capitalize()
                result["resourceId"] = f"gid://shopify/{resource_type}/{dest_id}"
            else:
                logger.warning(
                    f"[load] menus: {item_type} {src_id} not in ID map — "
                    f"falling back to URL for '{item.get('title')}'"
                )
                result["type"] = "URL"
                result["url"] = self._rewrite_url(url)
        else:
            result["url"] = self._rewrite_url(url)

        # Recurse into sub-items
        children = item.get("items", [])
        if children:
            result["items"] = [self._remap_item(child) for child in children]

        return result

    def _rewrite_url(self, url: str) -> str:
        """Replace source domain with dest domain in absolute URLs."""
        if self._source_domain and self._dest_domain:
            url = url.replace(self._source_domain, self._dest_domain)
        return url

    async def _create(self, payload: dict) -> str:
        menu = payload
        items_input = [self._remap_item(item) for item in menu.get("items", [])]

        data = await self.dest.graphql(
            _GQL_CREATE_MENU,
            variables={
                "title": menu.get("title"),
                "handle": menu.get("handle"),
                "items": items_input,
            },
            estimated_cost=150,
        )

        result = data.get("menuCreate", {})
        errors = result.get("userErrors", [])
        if errors:
            raise RuntimeError(f"menuCreate errors: {errors}")

        dest_gid = result["menu"]["id"]
        return dest_gid.split("/")[-1]

    async def find_existing(self, item: dict) -> Optional[dict]:
        handle = item.get("handle")
        if not handle:
            return None
        data = await self.dest.graphql(
            _GQL_GET_MENU_BY_HANDLE,
            variables={"handle": handle},
            estimated_cost=50,
        )
        edges = data.get("menus", {}).get("edges", [])
        return edges[0]["node"] if edges else None

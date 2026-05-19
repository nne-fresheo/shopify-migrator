from __future__ import annotations

import logging
from typing import Optional

from .base import BaseResource, _BASE_STRIP_FIELDS

logger = logging.getLogger(__name__)

_VARIANT_STRIP = _BASE_STRIP_FIELDS | {
    "product_id",
    "inventory_item_id",
    "fulfillment_service",
}
_IMAGE_STRIP = _BASE_STRIP_FIELDS | {"product_id", "variant_ids"}

_GQL_PRODUCTS = """
query fetchProducts($cursor: String) {
  products(first: 50, after: $cursor, sortKey: ID) {
    edges {
      node {
        id
        title
        handle
        status
        descriptionHtml
        vendor
        productType
        tags
        publishedAt
        templateSuffix
        options {
          name
          position
          values
        }
        variants(first: 100) {
          edges {
            node {
              id
              sku
              title
              price
              compareAtPrice
              position
              inventoryPolicy
              inventoryManagement
              barcode
              weight
              weightUnit
              requiresShipping
              taxable
              selectedOptions { name value }
            }
          }
        }
        images(first: 50) {
          edges {
            node {
              url
              altText
              width
              height
            }
          }
        }
        metafields(first: 30) {
          edges {
            node {
              namespace
              key
              value
              type
            }
          }
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""


def _gid_to_id(gid: str) -> str:
    """Convert 'gid://shopify/Product/123' → '123'."""
    return gid.rsplit("/", 1)[-1]


def _node_to_rest(node: dict) -> dict:
    """Convert a GraphQL product node to the REST-compatible shape used by transform()."""
    variants = []
    for i, edge in enumerate(node.get("variants", {}).get("edges", []), start=1):
        v = edge["node"]
        opts = {o["name"]: o["value"] for o in v.get("selectedOptions", [])}
        variants.append({
            "id": int(_gid_to_id(v["id"])),
            "title": v.get("title", ""),
            "sku": v.get("sku", ""),
            "price": v.get("price", "0.00"),
            "compare_at_price": v.get("compareAtPrice"),
            "position": v.get("position", i),
            "inventory_policy": v.get("inventoryPolicy", "deny").lower(),
            "inventory_management": v.get("inventoryManagement"),
            "barcode": v.get("barcode"),
            "weight": v.get("weight"),
            "weight_unit": v.get("weightUnit", "kg").lower(),
            "requires_shipping": v.get("requiresShipping", True),
            "taxable": v.get("taxable", True),
            "option1": opts.get(node.get("options", [{}])[0].get("name")) if len(node.get("options", [])) > 0 else None,
            "option2": opts.get(node.get("options", [{}])[1].get("name")) if len(node.get("options", [])) > 1 else None,
            "option3": opts.get(node.get("options", [{}])[2].get("name")) if len(node.get("options", [])) > 2 else None,
        })

    images = []
    for pos, edge in enumerate(node.get("images", {}).get("edges", []), start=1):
        img = edge["node"]
        images.append({
            "src": img.get("url", ""),
            "position": pos,
            "alt": img.get("altText", ""),
        })

    metafields = [
        {
            "namespace": mf["node"]["namespace"],
            "key": mf["node"]["key"],
            "value": mf["node"]["value"],
            "type": mf["node"]["type"],
        }
        for mf in node.get("metafields", {}).get("edges", [])
    ]

    options = [
        {"name": o["name"], "position": o["position"], "values": o["values"]}
        for o in node.get("options", [])
    ]

    return {
        "id": int(_gid_to_id(node["id"])),
        "title": node.get("title", ""),
        "handle": node.get("handle", ""),
        "status": node.get("status", "active").lower(),
        "body_html": node.get("descriptionHtml", ""),
        "vendor": node.get("vendor", ""),
        "product_type": node.get("productType", ""),
        "tags": ", ".join(node.get("tags", [])),
        "published_at": node.get("publishedAt"),
        "template_suffix": node.get("templateSuffix"),
        "options": options,
        "variants": variants,
        "images": images,
        "metafields": metafields,
    }


class ProductsResource(BaseResource):
    resource_name = "products"
    endpoint = "products.json"
    resource_key = "product"
    list_key = "products"

    async def _fetch_all(self) -> list[dict]:
        # First try REST
        all_items: list[dict] = []
        async for page in self.source.get_paginated(
            "products.json", "products", params={"status": "any"}
        ):
            all_items.extend(page)
            logger.debug(f"[extract] products: {len(all_items)} fetched so far")

        if all_items:
            return all_items

        # REST returned 0 — check the count endpoint to understand why
        count_response = await self.source.get("products/count.json", params={"status": "any"})
        count = count_response.get("count", 0)
        logger.warning(
            f"[extract] products: REST products.json returned 0 items "
            f"(products/count.json reports {count} products). "
            f"{'Falling back to GraphQL.' if count > 0 else 'Store appears to have no products.'}"
        )

        if count == 0:
            return []

        # GraphQL fallback — fetch all products via Admin GraphQL API
        logger.info("[extract] products: fetching via GraphQL Admin API")
        products: list[dict] = []
        cursor = None

        while True:
            variables = {"cursor": cursor} if cursor else {}
            data = await self.source.graphql(_GQL_PRODUCTS, variables=variables, estimated_cost=200)
            page_data = data.get("products", {})
            edges = page_data.get("edges", [])

            for edge in edges:
                products.append(_node_to_rest(edge["node"]))

            logger.debug(f"[extract] products (GraphQL): {len(products)} fetched so far")

            page_info = page_data.get("pageInfo", {})
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")

        logger.info(f"[extract] products: GraphQL fetch complete ({len(products)} products)")
        return products

    def transform(self, item: dict) -> dict:
        payload = {k: v for k, v in item.items() if k not in _BASE_STRIP_FIELDS}

        # Clean variants
        payload["variants"] = [
            {k: v for k, v in variant.items() if k not in _VARIANT_STRIP}
            for variant in payload.get("variants", [])
        ]

        # Clean images — keep src URL for Shopify to re-fetch from source CDN
        cleaned_images = []
        for img in payload.get("images", []):
            src = img.get("src")
            if src:
                cleaned_images.append(
                    {"src": src, "position": img.get("position"), "alt": img.get("alt")}
                )
        payload["images"] = cleaned_images

        return payload

    async def find_existing(self, item: dict) -> Optional[dict]:
        handle = item.get("handle")
        if not handle:
            return None
        response = await self.dest.get("products.json", params={"handle": handle})
        products = response.get("products", [])
        return products[0] if products else None

from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Optional

from ..client import ShopifyClient
from ..django_db import fetch_meals
from ..id_map import IDMap
from ..logger import FailedResourcesLog
from ..progress import ProgressTracker
from ..template import DescriptionRenderer
from .base import BaseResource

logger = logging.getLogger(__name__)


def _slugify(title: str) -> str:
    """Deterministic Shopify-compatible handle. Same input → same handle every
    run, so re-runs against the same Django data find existing products via
    `GET /products.json?handle=…` instead of triggering duplicate creates.

    Rules:
      1. Drop apostrophes (both straight U+0027 and curly U+2019) entirely so
         "l'avoine" and "l'avoine" collapse to the same slug.
      2. NFKD-decompose, drop non-ASCII bytes — strips remaining accents.
      3. Lowercase.
      4. Replace any run of non-alphanumeric chars with a single dash; strip
         leading/trailing dashes.
      5. Truncate to 255 chars (Shopify's handle limit).
    """
    no_apostrophe = title.replace("'", "").replace("’", "")
    normalized = unicodedata.normalize("NFKD", no_apostrophe)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_only.lower()
    dashed = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return dashed[:255]


# Fragment of fields the diff path needs. Shared by find-by-title and
# get-by-id so the rest of the code consumes one canonical shape.
_GQL_PRODUCT_FIELDS = """
  id
  legacyResourceId
  title
  bodyHtml
  tags
  productType
  category { id }
  options { id name position values }
  variants(first: 20) {
    edges { node {
      id legacyResourceId sku price
      inventoryItem { tracked }
      selectedOptions { name value }
    } }
  }
  images(first: 50) { edges { node { id } } }
  metafield(namespace: "fresheo", key: "image_sources") { value }
  resourcePublicationsV2(first: 50) {
    edges { node { publication { id } isPublished } }
  }
  sellingPlanGroups(first: 10) { edges { node { id } } }
"""

_GQL_FIND_BY_TITLE = """
query findByTitle($query: String!) {
  products(first: 2, query: $query) {
    edges {
      node {
""" + _GQL_PRODUCT_FIELDS + """
      }
    }
  }
}
"""

_GQL_GET_PRODUCT_BY_ID = """
query getProductById($id: ID!) {
  product(id: $id) {
""" + _GQL_PRODUCT_FIELDS + """
  }
}
"""


def _gid_to_id(gid: str) -> str:
    """Convert 'gid://shopify/Product/123' → '123'."""
    return gid.rsplit("/", 1)[-1]


def _escape_for_query(title: str) -> str:
    """Escape backslashes and double-quotes for use inside a Shopify GraphQL
    search query value wrapped in double-quotes."""
    return title.replace("\\", "\\\\").replace('"', '\\"')


def _parse_image_sources(value: str) -> list[str]:
    """Decode the `fresheo.image_sources` metafield value into a URL list.

    Shopify's `list.url` metafields serialize as a JSON array string. We also
    accept plain CSV for tolerance against earlier-run formats."""
    if not value:
        return []
    stripped = value.strip()
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list):
                return [str(u) for u in parsed]
        except json.JSONDecodeError:
            pass
    return [s.strip() for s in stripped.split(",") if s.strip()]


def _serialize_image_sources(urls: list[str]) -> str:
    """Inverse of [[_parse_image_sources]] — emits JSON suitable for the
    `list.url` metafield type."""
    return json.dumps(list(urls))


def _parse_tags_csv(value: str) -> set[str]:
    """Tags CSV (Shopify's REST shape, e.g. 'a, b, c') → set."""
    return {t.strip() for t in (value or "").split(",") if t.strip()}


def _diff_product(existing: dict, payload: dict) -> dict:
    """Return a dict of REST-shaped fields whose new value differs from the
    existing product. Empty dict ⇒ nothing to PUT.

    Comparisons:
      - title / body_html / product_type: string equality.
      - tags: CSV set equality (order/whitespace insensitive).
      - variants: keyed by sku on (price, option1); any mismatch emits the full
        new variants list. Inventory tracking is intentionally NOT compared
        here — it's handled out-of-band via GraphQL (see
        [[_needs_tracking_disable]] / [[MealsResource._disable_inventory_tracking]])
        because the REST inventory_management field was removed in API 2024-04.
      - options: [(name, sorted(values))] tuple equality; any mismatch emits
        the full new options list.
    """
    changed: dict = {}

    for key in ("title", "body_html", "product_type"):
        new_val = payload.get(key, "") or ""
        old_val = existing.get(key, "") or ""
        if new_val != old_val:
            changed[key] = new_val

    new_tags = _parse_tags_csv(payload.get("tags", ""))
    old_tags = set(existing.get("tags") or [])
    if new_tags != old_tags:
        changed["tags"] = payload.get("tags", "")

    if _variants_differ(existing.get("variants") or [], payload.get("variants") or []):
        changed["variants"] = payload.get("variants", [])

    # Only diff options when the payload carries some. Single-variant
    # categories (DESSERT, DRINKS, …) send no `options`; Shopify will keep
    # the implicit `[{Title: Default Title}]` and rejects `options: []`
    # outright with `could not update options to []`.
    new_options = payload.get("options") or []
    if new_options and _options_differ(existing.get("options") or [], new_options):
        changed["options"] = new_options

    return changed


def _variants_differ(existing: list[dict], new: list[dict]) -> bool:
    """Variants are 'equal' when keyed-by-SKU dicts match on price and option1.
    SKU is the stable cross-run key the migrator sets deterministically in
    [[_build_variants]]. Inventory tracking is excluded on purpose: a tracking
    mismatch must not trigger a full variant replacement (that would churn
    variant IDs and break variant↔image links) — it's reconciled separately via
    [[_needs_tracking_disable]] and a GraphQL productVariantsBulkUpdate."""
    def by_sku(items: list[dict]) -> dict[str, tuple]:
        return {
            (v.get("sku") or ""): (
                v.get("price"),
                v.get("option1"),
            )
            for v in items
        }
    return by_sku(existing) != by_sku(new)


def _needs_tracking_disable(existing_variants: list[dict]) -> bool:
    """True when any existing variant still has Shopify stock tracking enabled.

    [[MealsResource._normalize_graphql_node]] maps InventoryItem.tracked back to
    the REST-shaped `inventory_management` ('shopify' = tracked, None =
    untracked). Meals are made-to-order and must always be untracked, so a
    tracked variant means we owe a GraphQL productVariantsBulkUpdate to turn it
    off (see [[MealsResource._disable_inventory_tracking]])."""
    return any(
        v.get("inventory_management") == "shopify" for v in existing_variants
    )


def _options_differ(existing: list[dict], new: list[dict]) -> bool:
    def shape(options: list[dict]) -> list[tuple]:
        return [
            (o.get("name"), tuple(sorted(o.get("values") or [])))
            for o in options
        ]
    return shape(existing) != shape(new)


def _diff_images(existing_sources: list[str], new_sources: list[str]) -> bool:
    """True when the source-URL set differs (order-insensitive). When the
    metafield is absent on legacy products, existing is [] and any non-empty
    new set looks like a change — that forces a one-time refresh after this
    feature lands."""
    return set(existing_sources or []) != set(new_sources or [])


def _diff_publications(
    existing: dict[str, bool],
    all_pub_gids: list[str],
    should_be_published: bool,
) -> Optional[dict]:
    """Return {'mutation': 'publish'|'unpublish', 'pub_gids': [...]}
    describing the publications whose state needs to flip, or None when the
    current state already matches the target across every known publication.

    Conceptually: target = `should_be_published` on every pub_gid in
    `all_pub_gids`. We only emit the publications that don't already match.
    """
    if not all_pub_gids:
        return None
    to_flip = [
        gid for gid in all_pub_gids
        if bool(existing.get(gid, False)) != should_be_published
    ]
    if not to_flip:
        return None
    return {
        "mutation": "publish" if should_be_published else "unpublish",
        "pub_gids": to_flip,
    }


def _needs_subscription_association(
    existing_spg_ids: list[str], target_spg_gid: Optional[str],
) -> bool:
    """True when target SPG is configured and not already in the product's
    selling plan group list."""
    if not target_spg_gid:
        return False
    return target_spg_gid not in (existing_spg_ids or [])


_GQL_LIST_PUBLICATIONS = """
query listPublications {
  publications(first: 50) {
    edges { node { id name } }
  }
}
"""

_GQL_PUBLISH = """
mutation publish($id: ID!, $input: [PublicationInput!]!) {
  publishablePublish(id: $id, input: $input) {
    userErrors { field message }
  }
}
"""

_GQL_UNPUBLISH = """
mutation unpublish($id: ID!, $input: [PublicationInput!]!) {
  publishableUnpublish(id: $id, input: $input) {
    userErrors { field message }
  }
}
"""


_GQL_FIND_SELLING_PLAN_GROUP = """
query findSellingPlanGroup($query: String!) {
  sellingPlanGroups(first: 5, query: $query) {
    edges { node { id name merchantCode } }
  }
}
"""

_GQL_ADD_PRODUCTS_TO_SPG = """
mutation sellingPlanGroupAddProducts($id: ID!, $productIds: [ID!]!) {
  sellingPlanGroupAddProducts(id: $id, productIds: $productIds) {
    sellingPlanGroup { id }
    userErrors { field message }
  }
}
"""


_GQL_DISABLE_VARIANT_TRACKING = """
mutation disableTracking($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    userErrors { field message }
  }
}
"""


_GQL_SET_PRODUCT_CATEGORY = """
mutation setCategory($product: ProductUpdateInput!) {
  productUpdate(product: $product) {
    product { id category { id } }
    userErrors { field message }
  }
}
"""


class MealsResource(BaseResource):
    """Push Fresheo Django meals into the destination Shopify store.

    Differs from the other resources:
      - extracts from a Postgres DB (not Shopify) via `fetch_meals`
      - matches by exact product title via GraphQL (not handle)
      - updates `body_html` and replaces all images on existing products
      - creates new products when no title match is found
    """

    resource_name = "meals"
    endpoint = "products.json"
    resource_key = "product"
    list_key = "products"

    def __init__(
        self,
        *,
        source_client: Optional[ShopifyClient],
        dest_client: ShopifyClient,
        data_dir: Path,
        id_map: IDMap,
        progress: ProgressTracker,
        failed_log: FailedResourcesLog,
        dry_run: bool,
        renderer: DescriptionRenderer,
        django_dsn: str,
        django_media_url: str = "",
        locale: str = "fr",
        subscription_group_code: str = "",
    ) -> None:
        super().__init__(
            source_client=source_client,
            dest_client=dest_client,
            data_dir=data_dir,
            id_map=id_map,
            progress=progress,
            failed_log=failed_log,
            dry_run=dry_run,
        )
        self._renderer = renderer
        self._dsn = django_dsn
        self._media_url = django_media_url
        self._locale = locale
        # Lazy-loaded list of all Publication GIDs on the destination store.
        # Used to publish active meals everywhere / unpublish inactive ones.
        self._publication_gids: Optional[list[str]] = None
        # Loop subscription association — looked up once by merchantCode.
        # Sentinel `None` = uninitialized; `""` = lookup performed, not found.
        self._subscription_group_code = subscription_group_code
        self._subscription_group_gid: Optional[str] = None
        self._subscription_lookup_done: bool = False

    # ── EXTRACT ──────────────────────────────────────────────────────────────

    async def _fetch_all(self) -> list[dict]:
        if not self._dsn:
            raise RuntimeError("DJANGO_DATABASE_URL is not set")
        return await fetch_meals(
            self._dsn, locale=self._locale, media_url=self._media_url
        )

    # ── TRANSFORM ────────────────────────────────────────────────────────────

    def transform(self, item: dict) -> dict:
        # Whitespace-clean the title so the same Django data always slugifies
        # to the same handle (Django sometimes has trailing-space titles).
        title = (item.get("name") or "").strip()
        is_active = item.get("is_active_today", True)
        payload = {
            "title": title,
            "handle": _slugify(title),
            "body_html": self._renderer.render(item),
            "vendor": "Fresheo",
            "status": "active",
            # `published` controls Online Store auto-publish on create. For
            # inactive meals we set it false so the product isn't briefly
            # visible between POST and the explicit publishableUnpublish below.
            "published": is_active,
            "images": [{"src": url} for url in item.get("image_urls", [])],
        }
        category_label = item.get("category_label", "")
        if category_label:
            payload["product_type"] = category_label
        # Tags are migrator-managed: sent on every create AND update so the
        # `current-menu` tag flips off when a meal's active window expires.
        payload["tags"] = ", ".join(item.get("tags", []))
        # Variants and options are pre-shaped by django_db._build_variants —
        # multi-variant for MAIN_DISH/BREAKFAST (Size: Standard/Large), single
        # for everything else. Only emit `options` when variants carry option1,
        # otherwise Shopify rejects the product.
        payload["variants"] = item.get("variants") or []
        options = item.get("options") or []
        if options:
            payload["options"] = options
        # Track the source image URLs in a product metafield so re-runs can
        # diff what we *uploaded* against what we're *about to upload* without
        # parsing rewritten Shopify CDN URLs.
        image_urls = list(item.get("image_urls") or [])
        payload["metafields"] = [{
            "namespace": "fresheo",
            "key":       "image_sources",
            "type":      "list.url",
            "value":     _serialize_image_sources(image_urls),
        }]
        return payload

    # ── CUSTOM LOAD (upsert + image replacement) ─────────────────────────────

    async def _load_item(self, item: dict, force: bool = False) -> Optional[str]:
        source_id = str(item.get("meal_id", ""))
        title = item.get("name") or source_id

        if not force and self.id_map.has(source_id):
            logger.debug(f"[load] meal '{title}': already mapped, skipping")
            return self.id_map.get(source_id)

        if not force and self.progress.is_item_done(self.resource_name, title):
            logger.warning(f"[load] meal '{title}': already done, skipping")
            return None

        try:
            payload = self.transform(item)
            existing = await self._find_existing(payload["handle"], payload["title"])

            if self.dry_run:
                action = "update" if existing else "create"
                logger.info(f"[DRY RUN] would {action} meal '{title}'")
                return None

            if not existing:
                dest_id = await self._create(payload, title)
                logger.info(f"[load] meal '{title}': created (dest_id={dest_id})")
                product_gid = f"gid://shopify/Product/{dest_id}"
                # Assign the taxonomy category (drives Belgian VAT). REST create
                # can't set it, so it's a follow-up GraphQL productUpdate.
                await self._set_product_category(
                    product_gid, item.get("category_gid"), title
                )
                # New product: publication state + SPG association run once.
                await self._sync_publications(
                    product_gid, item.get("is_active_today", True), title
                )
                await self._associate_subscription(product_gid, title)
                self.id_map.set(source_id, dest_id)
                self.progress.mark_item_done(self.resource_name, title, dest_id)
                return dest_id

            # Update path — diff every concern, write only what changed.
            dest_id = existing["legacyResourceId"]
            product_gid = f"gid://shopify/Product/{dest_id}"

            body_diff = _diff_product(existing, payload)
            new_image_sources = list(item.get("image_urls") or [])
            images_changed = _diff_images(
                existing.get("image_sources") or [], new_image_sources
            )
            pub_diff = _diff_publications(
                existing.get("publications") or {},
                await self._get_publication_gids(),
                bool(item.get("is_active_today", True)),
            )
            spg_gid = await self._get_subscription_group_gid()
            needs_spg = _needs_subscription_association(
                existing.get("selling_plan_group_ids") or [], spg_gid,
            )
            # Any existing variant still tracked must be flipped off — meals are
            # made-to-order. This is its own concern (GraphQL), independent of
            # the variant body diff above.
            needs_tracking = _needs_tracking_disable(existing.get("variants") or [])
            # Taxonomy category (VAT). Only write when we have a desired node
            # and it differs — unmapped categories (desired None) are left
            # uncategorized on purpose, never cleared.
            desired_category = item.get("category_gid")
            needs_category = bool(desired_category) and (
                desired_category != existing.get("category_gid")
            )

            if not (body_diff or images_changed or pub_diff or needs_spg
                    or needs_tracking or needs_category):
                logger.info(
                    f"[load] meal '{title}': unchanged, skipping (dest_id={dest_id})"
                )
                self.id_map.set(source_id, dest_id)
                self.progress.mark_item_done(self.resource_name, title, dest_id)
                return dest_id

            # Start with pre-update variant IDs; if the sparse PUT replaced
            # variants, refresh them from the response so image-linking targets
            # the new IDs Shopify just assigned.
            variant_ids = [v["id"] for v in (existing.get("variants") or []) if v.get("id")]
            if body_diff:
                put_resp = await self._update_product_sparse(dest_id, body_diff, title)
                if "variants" in body_diff:
                    new_variants = (put_resp.get("product") or {}).get("variants") or []
                    variant_ids = [str(v["id"]) for v in new_variants if v.get("id")]
                    # Replaced variants are brand-new rows that inherit the
                    # store's tracked default — always re-disable on them.
                    needs_tracking = True
            if images_changed:
                await self._refresh_images(
                    dest_id, existing.get("image_ids") or [],
                    variant_ids, payload, title,
                )
            if pub_diff:
                await self._sync_publications_for(product_gid, pub_diff, title)
            if needs_spg:
                await self._associate_subscription(product_gid, title)
            if needs_tracking:
                await self._disable_inventory_tracking(
                    product_gid,
                    [f"gid://shopify/ProductVariant/{vid}" for vid in variant_ids],
                    title,
                )
            if needs_category:
                await self._set_product_category(
                    product_gid, desired_category, title
                )

            logger.info(f"[load] meal '{title}': updated (dest_id={dest_id})")
            self.id_map.set(source_id, dest_id)
            self.progress.mark_item_done(self.resource_name, title, dest_id)
            return dest_id

        except Exception as exc:
            logger.error(f"[load] meal '{title}': FAILED — {exc}")
            self.failed_log.append(
                resource_type=self.resource_name,
                source_id=source_id,
                handle=title,
                error=str(exc),
                payload=item,
            )
            self.progress.mark_item_failed(self.resource_name, title, str(exc))
            return None

    async def _find_existing(self, handle: str, title: str) -> Optional[dict]:
        """Idempotency lookup. Tries handle first (exact, fast, deterministic)
        then falls back to title search. After a positive REST handle hit we
        re-fetch via GraphQL by id to get the full diffable shape — keeps a
        single normalizer instead of two divergent ones.
        """
        resp = await self.dest.get("products.json", params={"handle": handle})
        rest_products = resp.get("products", [])
        if rest_products:
            product_gid = rest_products[0].get("admin_graphql_api_id")
            if not product_gid:
                product_gid = f"gid://shopify/Product/{rest_products[0]['id']}"
            data = await self.dest.graphql(
                _GQL_GET_PRODUCT_BY_ID,
                variables={"id": product_gid},
                estimated_cost=150,
            )
            node = data.get("product")
            if node:
                return self._normalize_graphql_node(node)

        # Fallback: GraphQL title search for legacy products without our slug.
        query = f'title:"{_escape_for_query(title)}"'
        data = await self.dest.graphql(
            _GQL_FIND_BY_TITLE, variables={"query": query}, estimated_cost=150
        )
        edges = data.get("products", {}).get("edges", [])
        if not edges:
            return None
        if len(edges) > 1:
            logger.warning(
                f"[load] meal '{title}': multiple Shopify products match this title; "
                f"using the first (id={edges[0]['node'].get('legacyResourceId')}). "
                f"Consider deleting duplicates manually."
            )
        return self._normalize_graphql_node(edges[0]["node"])

    @staticmethod
    def _normalize_graphql_node(node: dict) -> dict:
        """Project a Shopify GraphQL product node into the shape `_diff_*`
        helpers consume. Includes everything the update path might need to
        decide whether a write is required."""
        # Variants — match each existing variant by sku for diffing.
        variants: list[dict] = []
        for e in node.get("variants", {}).get("edges", []):
            v = e["node"]
            opts = v.get("selectedOptions") or []
            # `inventoryManagement` was removed in API 2024-04; the truth now
            # lives on InventoryItem.tracked. Map back to REST shape:
            # tracked=True ⇒ "shopify", tracked=False ⇒ None.
            tracked = (v.get("inventoryItem") or {}).get("tracked")
            inv_rest: Optional[str] = "shopify" if tracked else None
            variants.append({
                "id": str(v.get("legacyResourceId") or ""),
                "sku": v.get("sku") or "",
                "price": v.get("price"),
                "option1": opts[0]["value"] if opts else None,
                "inventory_management": inv_rest,
            })

        # Options — drop ids/positions; the diff only cares about (name, values).
        options = [
            {"name": o.get("name"), "values": list(o.get("values") or [])}
            for o in (node.get("options") or [])
        ]

        # Image sources metafield — stored as JSON list per Shopify's
        # `list.url` type, but tolerate plain CSV for back-compat.
        image_sources_value = (node.get("metafield") or {}).get("value") or ""
        image_sources = _parse_image_sources(image_sources_value)

        # Per-publication state.
        publications: dict[str, bool] = {}
        for e in node.get("resourcePublicationsV2", {}).get("edges", []):
            pub = e.get("node") or {}
            pub_gid = (pub.get("publication") or {}).get("id")
            if pub_gid:
                publications[pub_gid] = bool(pub.get("isPublished"))

        spg_ids = [
            e["node"]["id"]
            for e in node.get("sellingPlanGroups", {}).get("edges", [])
        ]

        return {
            "id":                     node["id"],
            "legacyResourceId":       str(node["legacyResourceId"]),
            "title":                  node.get("title", "") or "",
            "body_html":              node.get("bodyHtml", "") or "",
            "tags":                   list(node.get("tags") or []),
            "product_type":           node.get("productType", "") or "",
            "category_gid":           (node.get("category") or {}).get("id"),
            "options":                options,
            "variants":               variants,
            "image_ids": [
                _gid_to_id(e["node"]["id"])
                for e in node.get("images", {}).get("edges", [])
            ],
            "image_sources":          image_sources,
            "publications":           publications,
            "selling_plan_group_ids": spg_ids,
        }

    async def _update_product_sparse(
        self, dest_id: str, diff: dict, title: str,
    ) -> dict:
        """PUT only the fields that actually changed. `diff` already uses
        REST-shaped keys so it goes straight into the request body. Returns
        the Shopify response so callers can pull post-PUT variant IDs when
        the diff replaced variants."""
        body: dict = {"id": int(dest_id)}
        body.update(diff)
        response = await self.dest.put(
            f"products/{dest_id}.json", {"product": body},
        )
        logger.info(
            f"[load] meal '{title}': sparse PUT — changed fields: "
            f"{sorted(diff.keys())}"
        )
        return response

    async def _refresh_images(
        self,
        dest_id: str,
        existing_image_ids: list[str],
        variant_ids: list[str],
        payload: dict,
        title: str,
    ) -> None:
        """Replace every image on the product, linking each new image to every
        variant in `variant_ids` (so the variant page's image is set), then
        persist the source URL set in the `fresheo.image_sources` metafield."""
        for img_id in existing_image_ids:
            try:
                await self.dest.delete(f"products/{dest_id}/images/{img_id}.json")
            except Exception as exc:
                logger.warning(
                    f"[load] meal '{title}': failed to delete image {img_id}: {exc}"
                )
        for image in payload.get("images", []):
            body = dict(image)
            if variant_ids:
                body["variant_ids"] = [int(v) for v in variant_ids]
            try:
                await self.dest.post(
                    f"products/{dest_id}/images.json", {"image": body}
                )
            except Exception as exc:
                logger.warning(
                    f"[load] meal '{title}': failed to add image {image.get('src')}: {exc}"
                )
        await self._write_image_sources_metafield(dest_id, payload, title)

    async def _write_image_sources_metafield(
        self, dest_id: str, payload: dict, title: str,
    ) -> None:
        """Upsert the `fresheo.image_sources` metafield. The metafield
        definition is in `transform()`; we just re-POST it which Shopify
        treats as upsert by (namespace, key)."""
        metafields = payload.get("metafields") or []
        if not metafields:
            return
        try:
            await self.dest.post(
                f"products/{dest_id}/metafields.json",
                {"metafield": metafields[0]},
            )
        except Exception as exc:
            logger.warning(
                f"[load] meal '{title}': failed to write image_sources metafield: {exc}"
            )

    async def _create(self, payload: dict, title: str) -> str:
        """POST the product, disable inventory tracking on every freshly created
        variant, then link every variant to the (single) product image so the
        variant page's image is set."""
        response = await self.dest.post(self.endpoint, {self.resource_key: payload})
        resource = response.get(self.resource_key, {})
        dest_id = str(resource["id"])

        variant_ids = [str(v["id"]) for v in resource.get("variants") or []]
        # Variants Shopify just created inherit the store-level inventory
        # default (usually tracked). Meals are made-to-order — turn it off.
        await self._disable_inventory_tracking(
            f"gid://shopify/Product/{dest_id}",
            [f"gid://shopify/ProductVariant/{vid}" for vid in variant_ids],
            title,
        )
        for image in resource.get("images") or []:
            image_id = image.get("id")
            if not image_id or not variant_ids:
                continue
            try:
                await self.dest.put(
                    f"products/{dest_id}/images/{image_id}.json",
                    {"image": {
                        "id": int(image_id),
                        "variant_ids": [int(v) for v in variant_ids],
                    }},
                )
            except Exception as exc:
                logger.warning(
                    f"[load] meal dest_id={dest_id}: failed to link variants "
                    f"to image {image_id}: {exc}"
                )
        return dest_id

    async def _disable_inventory_tracking(
        self, product_gid: str, variant_gids: list[str], title: str,
    ) -> None:
        """Turn off Shopify stock tracking (`inventoryItem.tracked = false`) on
        every given variant via GraphQL.

        The REST `inventory_management` field was removed in API 2024-04, so on
        the version this store runs this mutation is the only way to disable
        tracking. Meals are made-to-order — without it they inherit the store
        default (tracked) and surface a false out-of-stock state. Idempotent:
        re-setting tracked=false is a no-op, and variant IDs are preserved so
        variant↔image links survive. Per-product failures are logged, not raised.
        """
        if not variant_gids:
            return
        variants_input = [
            {"id": gid, "inventoryItem": {"tracked": False}}
            for gid in variant_gids
        ]
        try:
            data = await self.dest.graphql(
                _GQL_DISABLE_VARIANT_TRACKING,
                variables={"productId": product_gid, "variants": variants_input},
                estimated_cost=50,
            )
            errors = (
                data.get("productVariantsBulkUpdate", {}).get("userErrors", [])
            )
            if errors:
                logger.warning(
                    f"[load] meal '{title}': disable-tracking userErrors: {errors}"
                )
        except Exception as exc:
            logger.warning(
                f"[load] meal '{title}': failed to disable inventory tracking: {exc}"
            )

    async def _set_product_category(
        self, product_gid: str, category_gid: Optional[str], title: str,
    ) -> None:
        """Assign the Shopify Standard Product Taxonomy category via GraphQL
        productUpdate. This category — not the free-text product_type — is what
        Shopify Tax reads to apply the Belgian VAT rate (food nodes → 6%).

        REST product create/update can't set the standard category, hence the
        separate mutation. A None `category_gid` means 'leave uncategorized'
        (→ 21% standard rate); we skip rather than clear. Per-product failures
        are logged, not raised."""
        if not category_gid:
            return
        try:
            data = await self.dest.graphql(
                _GQL_SET_PRODUCT_CATEGORY,
                variables={"product": {"id": product_gid, "category": category_gid}},
                estimated_cost=50,
            )
            errors = data.get("productUpdate", {}).get("userErrors", [])
            if errors:
                logger.warning(
                    f"[load] meal '{title}': set-category userErrors: {errors}"
                )
        except Exception as exc:
            logger.warning(
                f"[load] meal '{title}': failed to set taxonomy category "
                f"{category_gid}: {exc}"
            )

    # ── PUBLICATIONS ─────────────────────────────────────────────────────────

    async def _get_publication_gids(self) -> list[str]:
        """Fetch and cache the list of Publication GIDs on the destination store.

        Called once per sync-meals run. The list represents every sales channel
        the store has installed (Online Store, POS, Google, Meta, etc.).
        """
        if self._publication_gids is not None:
            return self._publication_gids

        data = await self.dest.graphql(_GQL_LIST_PUBLICATIONS, estimated_cost=10)
        edges = data.get("publications", {}).get("edges", [])
        gids = [e["node"]["id"] for e in edges]
        names = [e["node"]["name"] for e in edges]
        logger.info(
            f"[load] cached {len(gids)} publications on destination store: "
            f"{', '.join(names) if names else '(none)'}"
        )
        self._publication_gids = gids
        return gids

    async def _sync_publications(
        self, product_gid: str, is_active: bool, title: str
    ) -> None:
        """Publish the product to every store publication if active today,
        otherwise unpublish from every publication. Per-product failures are
        logged but don't abort the meal load."""
        pub_gids = await self._get_publication_gids()
        if not pub_gids:
            logger.warning(
                f"[load] meal '{title}': destination store has no publications; "
                f"skipping publish/unpublish"
            )
            return

        input_value = [{"publicationId": gid} for gid in pub_gids]
        mutation = _GQL_PUBLISH if is_active else _GQL_UNPUBLISH
        action = "publish" if is_active else "unpublish"
        try:
            await self.dest.graphql(
                mutation,
                variables={"id": product_gid, "input": input_value},
                estimated_cost=50,
            )
            past = "published" if is_active else "unpublished"
            logger.debug(
                f"[load] meal '{title}': {past} across {len(pub_gids)} publications"
            )
        except Exception as exc:
            logger.warning(
                f"[load] meal '{title}': failed to {action} ({product_gid}): {exc}"
            )

    async def _sync_publications_for(
        self, product_gid: str, pub_diff: dict, title: str,
    ) -> None:
        """Flip only the publications that don't already match the target
        state. `pub_diff` is the output of [[_diff_publications]]."""
        pub_gids = pub_diff["pub_gids"]
        if not pub_gids:
            return
        is_publish = pub_diff["mutation"] == "publish"
        mutation = _GQL_PUBLISH if is_publish else _GQL_UNPUBLISH
        action = "publish" if is_publish else "unpublish"
        input_value = [{"publicationId": gid} for gid in pub_gids]
        try:
            await self.dest.graphql(
                mutation,
                variables={"id": product_gid, "input": input_value},
                estimated_cost=50,
            )
            past = "published" if is_publish else "unpublished"
            logger.info(
                f"[load] meal '{title}': {past} on {len(pub_gids)} publication(s)"
            )
        except Exception as exc:
            logger.warning(
                f"[load] meal '{title}': failed to {action} ({product_gid}): {exc}"
            )

    # ── SUBSCRIPTION (Loop / selling plan group) ─────────────────────────────

    async def _get_subscription_group_gid(self) -> Optional[str]:
        """Fetch and cache the destination GID of the Loop selling plan group,
        identified by `merchantCode`. Logs once and returns None if not found
        so subsequent meals skip the association without re-querying."""
        if self._subscription_lookup_done:
            return self._subscription_group_gid

        self._subscription_lookup_done = True
        code = self._subscription_group_code
        if not code:
            return None

        data = await self.dest.graphql(
            _GQL_FIND_SELLING_PLAN_GROUP,
            variables={"query": f"merchant_code:{code}"},
            estimated_cost=20,
        )
        edges = data.get("sellingPlanGroups", {}).get("edges", [])
        # Shopify's `query:` is a prefix/substring search; verify the exact code.
        for edge in edges:
            node = edge["node"]
            if node.get("merchantCode") == code:
                self._subscription_group_gid = node["id"]
                logger.info(
                    f"[load] subscription group merchantCode={code!r} → {node['id']}"
                )
                return self._subscription_group_gid

        logger.warning(
            f"[load] subscription group with merchantCode={code!r} not found on "
            f"destination store; products will be synced without a purchase option"
        )
        return None

    async def _associate_subscription(
        self, product_gid: str, title: str
    ) -> None:
        """Attach the meal product to the Loop selling plan group so the
        storefront offers an Option d'achat. Idempotent — Shopify accepts the
        same association repeatedly without error."""
        group_gid = await self._get_subscription_group_gid()
        if not group_gid:
            return
        try:
            data = await self.dest.graphql(
                _GQL_ADD_PRODUCTS_TO_SPG,
                variables={"id": group_gid, "productIds": [product_gid]},
                estimated_cost=50,
            )
            errors = (
                data.get("sellingPlanGroupAddProducts", {}).get("userErrors", [])
            )
            if errors:
                logger.warning(
                    f"[load] meal '{title}': subscription association userErrors: {errors}"
                )
        except Exception as exc:
            logger.warning(
                f"[load] meal '{title}': failed to associate subscription group: {exc}"
            )

    # Unused by the upsert flow but required by BaseResource's abstract surface.
    async def find_existing(self, item: dict) -> Optional[dict]:  # pragma: no cover
        title = (item.get("name") or "").strip()
        return await self._find_existing(_slugify(title), title)

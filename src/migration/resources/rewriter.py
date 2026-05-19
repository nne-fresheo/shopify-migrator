from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from ..client import ShopifyClient
from ..id_map import IDMap

logger = logging.getLogger(__name__)

_CDN_RE = re.compile(r'https?://cdn\.shopify\.com/s/files/[^\s"\'<>]+')


class ImageUrlRewriter:
    """
    Post-processing pass: rewrites embedded CDN image URLs in product, page, and
    article HTML bodies.

    Run after both files and content (products, pages, blogs, articles) have been
    fully loaded. Reads data/id_maps/files.json which maps source URLs → dest URLs,
    then updates body_html on the destination store.

    Usage:
      python -m migration rewrite-images
    """

    def __init__(
        self,
        dest_client: ShopifyClient,
        data_dir: Path,
        files_id_map: IDMap,
        dry_run: bool = False,
    ) -> None:
        self._dest = dest_client
        self._data_dir = data_dir
        self._files_id_map = files_id_map
        self._dry_run = dry_run

        # Build URL replacement table: source_cdn_url → dest_cdn_url
        self._url_map: dict[str, str] = {}
        for src, dst in files_id_map.items():
            # The files id_map stores both ID mappings and URL mappings
            if src.startswith("http"):
                self._url_map[src] = dst

    def _rewrite_html(self, html: str) -> tuple[str, int]:
        """Replace source CDN URLs in HTML. Returns (new_html, replacement_count)."""
        count = 0

        def replace(match: re.Match) -> str:
            nonlocal count
            url = match.group(0)
            dest = self._url_map.get(url)
            if dest:
                count += 1
                return dest
            return url

        new_html = _CDN_RE.sub(replace, html or "")
        return new_html, count

    async def rewrite_products(self) -> None:
        products_file = self._data_dir / "products.json"
        if not products_file.exists():
            logger.info("[rewrite-images] products: no data file, skipping")
            return

        products: list[dict] = json.loads(products_file.read_text(encoding="utf-8"))
        logger.info(f"[rewrite-images] products: checking {len(products)} products")
        updated = 0

        for product in products:
            body = product.get("body_html", "")
            new_body, count = self._rewrite_html(body)
            if count == 0:
                continue

            if self._dry_run:
                logger.info(
                    f"[DRY RUN] would update {count} image URLs in product '{product.get('handle')}'"
                )
                continue

            try:
                response = await self._dest.get(
                    "products.json", params={"handle": product.get("handle")}
                )
                dest_products = response.get("products", [])
                if not dest_products:
                    continue
                dest_id = dest_products[0]["id"]
                await self._dest.put(
                    f"products/{dest_id}.json",
                    {"product": {"id": dest_id, "body_html": new_body}},
                )
                updated += 1
                logger.info(
                    f"[rewrite-images] updated {count} URLs in product '{product.get('handle')}'"
                )
            except Exception as exc:
                logger.error(
                    f"[rewrite-images] failed to update product '{product.get('handle')}': {exc}"
                )

        logger.info(f"[rewrite-images] products: done ({updated} updated)")

    async def rewrite_pages(self) -> None:
        pages_file = self._data_dir / "pages.json"
        if not pages_file.exists():
            logger.info("[rewrite-images] pages: no data file, skipping")
            return

        pages: list[dict] = json.loads(pages_file.read_text(encoding="utf-8"))
        logger.info(f"[rewrite-images] pages: checking {len(pages)} pages")
        updated = 0

        for page in pages:
            body = page.get("body_html", "")
            new_body, count = self._rewrite_html(body)
            if count == 0:
                continue

            if self._dry_run:
                logger.info(
                    f"[DRY RUN] would update {count} image URLs in page '{page.get('handle')}'"
                )
                continue

            try:
                # Find dest page by handle
                response = await self._dest.get(
                    "pages.json", params={"handle": page.get("handle")}
                )
                dest_pages = response.get("pages", [])
                if not dest_pages:
                    continue
                dest_id = dest_pages[0]["id"]
                await self._dest.put(
                    f"pages/{dest_id}.json",
                    {"page": {"id": dest_id, "body_html": new_body}},
                )
                updated += 1
                logger.info(
                    f"[rewrite-images] updated {count} URLs in page '{page.get('handle')}'"
                )
            except Exception as exc:
                logger.error(f"[rewrite-images] failed to update page '{page.get('handle')}': {exc}")

        logger.info(f"[rewrite-images] pages: done ({updated} updated)")

    async def rewrite_articles(self) -> None:
        articles_file = self._data_dir / "articles.json"
        if not articles_file.exists():
            logger.info("[rewrite-images] articles: no data file, skipping")
            return

        articles: list[dict] = json.loads(articles_file.read_text(encoding="utf-8"))
        logger.info(f"[rewrite-images] articles: checking {len(articles)} articles")
        updated = 0

        # Load blogs ID map to find dest blog IDs
        blogs_file = self._data_dir / "blogs.json"
        blogs_id_map_file = self._data_dir / "id_maps" / "blogs.json"
        blogs_id_map: dict[str, str] = {}
        if blogs_id_map_file.exists():
            blogs_id_map = json.loads(blogs_id_map_file.read_text(encoding="utf-8"))

        for article in articles:
            body = article.get("body_html", "")
            new_body, count = self._rewrite_html(body)
            if count == 0:
                continue

            if self._dry_run:
                logger.info(
                    f"[DRY RUN] would update {count} image URLs in article '{article.get('handle')}'"
                )
                continue

            try:
                src_blog_id = str(article.get("_source_blog_id", ""))
                dest_blog_id = blogs_id_map.get(src_blog_id)
                if not dest_blog_id:
                    continue

                response = await self._dest.get(
                    f"blogs/{dest_blog_id}/articles.json",
                    params={"handle": article.get("handle")},
                )
                dest_articles = response.get("articles", [])
                if not dest_articles:
                    continue
                dest_id = dest_articles[0]["id"]
                await self._dest.put(
                    f"blogs/{dest_blog_id}/articles/{dest_id}.json",
                    {"article": {"id": dest_id, "body_html": new_body}},
                )
                updated += 1
                logger.info(
                    f"[rewrite-images] updated {count} URLs in article '{article.get('handle')}'"
                )
            except Exception as exc:
                logger.error(
                    f"[rewrite-images] failed to update article '{article.get('handle')}': {exc}"
                )

        logger.info(f"[rewrite-images] articles: done ({updated} updated)")

    async def run(self) -> None:
        if not self._url_map:
            logger.warning(
                "[rewrite-images] files ID map has no URL entries. "
                "Run 'migrate files' first."
            )
            return
        logger.info(
            f"[rewrite-images] starting with {len(self._url_map)} URL mappings"
        )
        await self.rewrite_products()
        await self.rewrite_pages()
        await self.rewrite_articles()
        logger.info("[rewrite-images] done")

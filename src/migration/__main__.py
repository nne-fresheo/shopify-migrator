from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table

from .config import load_config, Config
from .client import ShopifyClient
from .id_map import IDMapRegistry
from .logger import FailedResourcesLog, setup_logging
from .progress import ProgressTracker
from .resources.articles import ArticlesResource
from .resources.blogs import BlogsResource
from .resources.collections import CollectionsResource
from .resources.discount_codes import DiscountCodesResource
from .resources.files import FilesResource
from .resources.gift_cards import GiftCardsResource
from .resources.inventory import InventoryResource
from .resources.menus import MenusResource
from .resources.pages import PagesResource
from .resources.price_rules import PriceRulesResource
from .resources.products import ProductsResource
from .resources.redirects import RedirectsResource
from .resources.meals import MealsResource
from .resources.rewriter import ImageUrlRewriter
from .resources.selling_plans import SellingPlansResource
from .resources.variant_id_map_builder import build_variant_id_map
from .template import DescriptionRenderer

console = Console()

# Canonical resource execution order
RESOURCE_ORDER = [
    "files",
    "products",
    "collections",
    "inventory",
    "pages",
    "blogs",
    "articles",
    "redirects",
    "price_rules",
    "discount_codes",
    "gift_cards",
    "selling_plans",
    "menus",
]


def _make_client(cfg: Config, shop: str, token: str, client_id: str, client_secret: str) -> ShopifyClient:
    return ShopifyClient(
        shop_domain=shop,
        access_token=token,
        api_version=cfg.api_version,
        rest_bucket_size=cfg.rest_bucket_size,
        rest_refill_rate=cfg.rest_refill_rate,
        graphql_max_cost=cfg.graphql_max_cost,
        graphql_restore_rate=cfg.graphql_restore_rate,
        graphql_cost_threshold=cfg.graphql_cost_threshold,
        max_retries=cfg.max_retries,
        client_id=client_id,
        client_secret=client_secret,
    )


def _build_resources(
    cfg: Config,
    source: ShopifyClient,
    dest: ShopifyClient,
    registry: IDMapRegistry,
    progress: ProgressTracker,
    failed_log: FailedResourcesLog,
    dry_run: bool,
    variants_id_map: Optional["IDMap"] = None,
) -> dict:
    """Instantiate all resource objects."""

    def _make(cls, **extra):
        return cls(
            source_client=source,
            dest_client=dest,
            data_dir=cfg.data_dir,
            id_map=registry.get(cls.resource_name),
            progress=progress,
            failed_log=failed_log,
            dry_run=dry_run,
            **extra,
        )

    return {
        "files": _make(FilesResource),
        "products": _make(ProductsResource),
        "collections": _make(
            CollectionsResource,
            products_id_map=registry.get("products"),
        ),
        "inventory": _make(
            InventoryResource,
            products_id_map=registry.get("products"),
        ),
        "pages": _make(PagesResource),
        "blogs": _make(BlogsResource),
        "articles": _make(
            ArticlesResource,
            blogs_id_map=registry.get("blogs"),
        ),
        "redirects": _make(RedirectsResource),
        "price_rules": _make(
            PriceRulesResource,
            collections_id_map=registry.get("collections"),
            variants_id_map=variants_id_map,
        ),
        "discount_codes": _make(
            DiscountCodesResource,
            price_rules_id_map=registry.get("price_rules"),
        ),
        "gift_cards": _make(GiftCardsResource),
        "selling_plans": _make(
            SellingPlansResource,
            products_id_map=registry.get("products"),
        ),
        "menus": _make(
            MenusResource,
            products_id_map=registry.get("products"),
            collections_id_map=registry.get("collections"),
            pages_id_map=registry.get("pages"),
            blogs_id_map=registry.get("blogs"),
            articles_id_map=registry.get("articles"),
            source_domain=cfg.source_shop,
            dest_domain=cfg.dest_shop,
        ),
    }


async def _build_variants_id_map(
    cfg: Config,
    source: ShopifyClient,
    dest: ShopifyClient,
) -> "IDMap":
    """Build (or load from cache) the variant SKU-based ID map."""
    from .id_map import IDMap
    cache_path = cfg.id_maps_dir / "variants.json"
    # build_variant_id_map handles atomic save; load with IDMap to avoid per-set file writes
    await build_variant_id_map(source, dest, cache_path=cache_path)
    return IDMap(cache_path)


def _resolve_resources(resource: str) -> list[str]:
    if resource == "all":
        return RESOURCE_ORDER
    if resource not in RESOURCE_ORDER:
        raise click.BadParameter(
            f"Unknown resource '{resource}'. Valid: {', '.join(RESOURCE_ORDER)}"
        )
    return [resource]


# ── CLI ───────────────────────────────────────────────────────────────────────

@click.group()
def cli() -> None:
    """Shopify store migration tool."""
    pass


@cli.command()
@click.option("--resource", default="all", help="Resource to extract (default: all)")
@click.option("--force", is_flag=True, help="Re-extract even if data file already exists")
def extract(resource: str, force: bool) -> None:
    """Phase 1: extract all resources from source store to data/ directory."""
    asyncio.run(_run_extract(resource, force))


@cli.command()
@click.option("--resource", default="all", help="Resource to load (default: all)")
@click.option("--dry-run", is_flag=True, help="Simulate load without writing to destination")
@click.option("--force", is_flag=True, help="Re-create even if resource already mapped")
def load(resource: str, dry_run: bool, force: bool) -> None:
    """Phase 2: load resources from data/ directory to destination store."""
    asyncio.run(_run_load(resource, dry_run, force))


@cli.command()
@click.option("--resource", default="all", help="Resource to migrate (default: all)")
@click.option("--dry-run", is_flag=True, help="Simulate load without writing to destination")
@click.option("--force", is_flag=True, help="Force re-extract and re-create")
def migrate(resource: str, dry_run: bool, force: bool) -> None:
    """Run extract then load sequentially."""
    asyncio.run(_run_migrate(resource, dry_run, force))


@cli.command()
def status() -> None:
    """Show migration progress summary."""
    asyncio.run(_run_status())


@cli.command("rewrite-images")
@click.option("--dry-run", is_flag=True, help="Show what would be rewritten without making changes")
def rewrite_images(dry_run: bool) -> None:
    """Post-processing: rewrite embedded CDN image URLs in pages and articles."""
    asyncio.run(_run_rewrite_images(dry_run))


@cli.command("sync-meals")
@click.option("--dry-run", is_flag=True, help="Show what would be synced without writing")
@click.option("--force", is_flag=True, help="Re-process meals even if already done in progress.json")
@click.option("--skip-extract", is_flag=True, help="Reuse existing data/meals.json instead of re-querying Postgres")
def sync_meals(dry_run: bool, force: bool, skip_extract: bool) -> None:
    """Sync meals from the Fresheo Django backend into the destination Shopify store."""
    asyncio.run(_run_sync_meals(dry_run, force, skip_extract))


# ── ASYNC RUNNERS ─────────────────────────────────────────────────────────────

async def _run_extract(resource: str, force: bool) -> None:
    cfg = load_config()
    setup_logging(cfg.migration_log_file, cfg.log_level)
    resource_names = _resolve_resources(resource)

    async with (
        _make_client(cfg, cfg.source_shop, cfg.source_token, cfg.source_client_id, cfg.source_client_secret) as source,
        _make_client(cfg, cfg.dest_shop, cfg.dest_token, cfg.dest_client_id, cfg.dest_client_secret) as dest,
    ):
        registry = IDMapRegistry(cfg.id_maps_dir)
        progress = ProgressTracker(cfg.progress_file)
        failed_log = FailedResourcesLog(cfg.failed_resources_file)
        resources = _build_resources(cfg, source, dest, registry, progress, failed_log, False)

        for name in resource_names:
            await resources[name].extract(force=force)


async def _run_load(resource: str, dry_run: bool, force: bool) -> None:
    cfg = load_config()
    setup_logging(cfg.migration_log_file, cfg.log_level)
    resource_names = _resolve_resources(resource)

    if dry_run:
        console.print("[bold yellow]DRY RUN MODE — no writes will be made to destination[/]")

    async with (
        _make_client(cfg, cfg.source_shop, cfg.source_token, cfg.source_client_id, cfg.source_client_secret) as source,
        _make_client(cfg, cfg.dest_shop, cfg.dest_token, cfg.dest_client_id, cfg.dest_client_secret) as dest,
    ):
        registry = IDMapRegistry(cfg.id_maps_dir)
        progress = ProgressTracker(cfg.progress_file)
        failed_log = FailedResourcesLog(cfg.failed_resources_file)

        variants_id_map = None
        if "price_rules" in resource_names:
            variants_id_map = await _build_variants_id_map(cfg, source, dest)

        resources = _build_resources(
            cfg, source, dest, registry, progress, failed_log, dry_run,
            variants_id_map=variants_id_map,
        )

        for name in resource_names:
            resource_obj = resources[name]
            await resource_obj.load(force=force)

            # Special post-load step for collections: load memberships after products+collections
            if name == "collections":
                await resource_obj.load_memberships(force=force)


async def _run_migrate(resource: str, dry_run: bool, force: bool) -> None:
    cfg = load_config()
    setup_logging(cfg.migration_log_file, cfg.log_level)
    resource_names = _resolve_resources(resource)

    if dry_run:
        console.print("[bold yellow]DRY RUN MODE — no writes will be made to destination[/]")

    async with (
        _make_client(cfg, cfg.source_shop, cfg.source_token, cfg.source_client_id, cfg.source_client_secret) as source,
        _make_client(cfg, cfg.dest_shop, cfg.dest_token, cfg.dest_client_id, cfg.dest_client_secret) as dest,
    ):
        registry = IDMapRegistry(cfg.id_maps_dir)
        progress = ProgressTracker(cfg.progress_file)
        failed_log = FailedResourcesLog(cfg.failed_resources_file)

        variants_id_map = None
        if "price_rules" in resource_names:
            variants_id_map = await _build_variants_id_map(cfg, source, dest)

        resources = _build_resources(
            cfg, source, dest, registry, progress, failed_log, dry_run,
            variants_id_map=variants_id_map,
        )

        for name in resource_names:
            resource_obj = resources[name]
            await resource_obj.extract(force=force)
            await resource_obj.load(force=force)
            if name == "collections":
                await resource_obj.load_memberships(force=force)


async def _run_status() -> None:
    cfg = load_config()
    setup_logging(cfg.migration_log_file, cfg.log_level)
    progress = ProgressTracker(cfg.progress_file)
    summary = progress.get_summary()

    # Load data files to count totals
    totals: dict[str, int] = {}
    for name in RESOURCE_ORDER:
        data_file = cfg.data_dir / f"{name}.json"
        if data_file.exists():
            try:
                data = json.loads(data_file.read_text(encoding="utf-8"))
                totals[name] = len(data)
            except Exception:
                totals[name] = 0
        else:
            totals[name] = 0

    table = Table(title="Migration Status", show_lines=True)
    table.add_column("Resource", style="cyan")
    table.add_column("Total", justify="right")
    table.add_column("Loaded", justify="right", style="green")
    table.add_column("Failed", justify="right", style="red")
    table.add_column("Pending", justify="right", style="yellow")
    table.add_column("Status")

    for name in RESOURCE_ORDER:
        info = summary.get(name, {})
        total = totals.get(name, 0)
        loaded = info.get("loaded", 0)
        failed = info.get("failed", 0)
        pending = max(0, total - loaded - failed)
        status = info.get("status", "not started")
        status_style = "green" if status == "done" else "yellow" if status == "pending" else "white"

        table.add_row(
            name,
            str(total),
            str(loaded),
            str(failed),
            str(pending),
            f"[{status_style}]{status}[/]",
        )

    console.print(table)

    # Show failed resources summary
    failed_file = cfg.failed_resources_file
    if failed_file.exists():
        try:
            failed_entries = json.loads(failed_file.read_text(encoding="utf-8"))
            if failed_entries:
                console.print(
                    f"\n[red]{len(failed_entries)} failed resources logged to "
                    f"{failed_file}[/red]"
                )
        except Exception:
            pass


async def _run_rewrite_images(dry_run: bool) -> None:
    cfg = load_config()
    setup_logging(cfg.migration_log_file, cfg.log_level)

    if dry_run:
        console.print("[bold yellow]DRY RUN MODE[/]")

    async with _make_client(cfg, cfg.dest_shop, cfg.dest_token, cfg.dest_client_id, cfg.dest_client_secret) as dest:
        registry = IDMapRegistry(cfg.id_maps_dir)
        files_id_map = registry.get("files")
        rewriter = ImageUrlRewriter(
            dest_client=dest,
            data_dir=cfg.data_dir,
            files_id_map=files_id_map,
            dry_run=dry_run,
        )
        await rewriter.run()


async def _run_sync_meals(dry_run: bool, force: bool, skip_extract: bool) -> None:
    cfg = load_config()
    setup_logging(cfg.migration_log_file, cfg.log_level)

    if not cfg.django_database_url and not skip_extract:
        raise click.ClickException(
            "DJANGO_DATABASE_URL is not set. Add it to .env or use --skip-extract "
            "to reuse an existing data/meals.json."
        )

    renderer = DescriptionRenderer(cfg.description_template)

    if dry_run:
        console.print("[bold yellow]DRY RUN MODE — no writes will be made to destination[/]")

    async with _make_client(
        cfg, cfg.dest_shop, cfg.dest_token, cfg.dest_client_id, cfg.dest_client_secret
    ) as dest:
        registry = IDMapRegistry(cfg.id_maps_dir)
        progress = ProgressTracker(cfg.progress_file)
        failed_log = FailedResourcesLog(cfg.failed_resources_file)

        resource = MealsResource(
            source_client=None,
            dest_client=dest,
            data_dir=cfg.data_dir,
            id_map=registry.get("meals"),
            progress=progress,
            failed_log=failed_log,
            dry_run=dry_run,
            renderer=renderer,
            django_dsn=cfg.django_database_url,
            django_media_url=cfg.django_media_url,
            locale=cfg.meal_locale,
            subscription_group_code=cfg.subscription_group_code,
        )

        await resource.extract(force=force and not skip_extract)
        await resource.load(force=force)


if __name__ == "__main__":
    cli()

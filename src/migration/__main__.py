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
from .menu_autofill.autofill import MenuAutofiller, write_audit
from .menu_autofill.loop_client import LoopAdminClient, LoopStorefrontClient
from .template import DescriptionRenderer
from .vouchers import (
    VoucherGenerator,
    VoucherUpdater,
    _UPDATE_REPORT_FIELDS,
    read_emails,
    read_report,
    write_report,
)

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


@cli.command("unpublish-inactive-meals")
@click.option("--dry-run", is_flag=True, help="List products that would be unpublished without writing")
def unpublish_inactive_meals(dry_run: bool) -> None:
    """One-time backfill: unpublish meal products no longer in the active menu.

    Targets meals that left the menu before the reconcile pass learned to
    unpublish (they lost their 'current-menu' tag but stayed on all sales
    channels). Reads from Shopify only — no Django DB needed. Run with
    --dry-run first to review the count.
    """
    asyncio.run(_run_unpublish_inactive_meals(dry_run))


@cli.command("generate-vouchers")
@click.option(
    "--file", "email_file",
    type=click.Path(exists=True, dir_okay=False),
    required=True,
    help="Excel (.xlsx) file with an 'email' column",
)
@click.option("--amount", type=float, required=True, help="Reduction in euros (e.g. 10)")
@click.option("--days", type=int, required=True, help="Days the voucher stays valid from now")
@click.option("--prefix", default="FRESHEO", help="Discount code prefix (default: FRESHEO)")
@click.option("--column", default="email", help="Header of the email column (default: email)")
@click.option(
    "--purchase-type",
    type=click.Choice(["one-time", "subscription", "both"]),
    default="both",
    help="Whether the voucher applies to one-time purchases, subscriptions, or both (default: both)",
)
@click.option(
    "--recurring-cycles",
    type=int,
    default=1,
    help="For subscriptions: billing cycles the discount applies to "
    "(1 = first order only, 0 = every recurring order). Default: 1",
)
@click.option(
    "--output", "output_file",
    type=click.Path(dir_okay=False),
    default="vouchers.csv",
    help="CSV report path (default: vouchers.csv)",
)
@click.option("--dry-run", is_flag=True, help="Show what would be created without writing")
def generate_vouchers(
    email_file: str,
    amount: float,
    days: int,
    prefix: str,
    column: str,
    purchase_type: str,
    recurring_cycles: int,
    output_file: str,
    dry_run: bool,
) -> None:
    """Generate a per-email, customer-restricted voucher in the destination store."""
    asyncio.run(
        _run_generate_vouchers(
            email_file, amount, days, prefix, column,
            purchase_type, recurring_cycles, output_file, dry_run,
        )
    )


@cli.command("update-vouchers")
@click.option(
    "--file", "report_file",
    type=click.Path(exists=True, dir_okay=False),
    required=True,
    help="A vouchers.csv report from a previous generate-vouchers run",
)
@click.option("--amount", type=float, default=None, help="New euro reduction for every voucher")
@click.option("--days", type=int, default=None, help="New validity window: expiry becomes now + N days")
@click.option(
    "--purchase-type",
    type=click.Choice(["one-time", "subscription", "both"]),
    default=None,
    help="Change which purchase types the voucher applies to",
)
@click.option(
    "--recurring-cycles",
    type=int,
    default=None,
    help="Subscription billing cycles (1 = first order only, 0 = every recurring order)",
)
@click.option("--usage-limit", type=int, default=None, help="New total usage limit per voucher")
@click.option(
    "--once-per-customer/--no-once-per-customer",
    default=None,
    help="Set or clear the once-per-customer limit",
)
@click.option(
    "--output", "output_file",
    type=click.Path(dir_okay=False),
    default="vouchers_updated.csv",
    help="CSV report path (default: vouchers_updated.csv)",
)
@click.option("--dry-run", is_flag=True, help="Show what would be updated without writing")
def update_vouchers(
    report_file: str,
    amount: Optional[float],
    days: Optional[int],
    purchase_type: Optional[str],
    recurring_cycles: Optional[int],
    usage_limit: Optional[int],
    once_per_customer: Optional[bool],
    output_file: str,
    dry_run: bool,
) -> None:
    """Update existing vouchers (by code) from a report CSV — only the fields you pass change."""
    asyncio.run(
        _run_update_vouchers(
            report_file, amount, days, purchase_type, recurring_cycles,
            usage_limit, once_per_customer, output_file, dry_run,
        )
    )


@cli.command("autofill-menu")
@click.option(
    "--dry-run/--no-dry-run",
    default=True,
    help="Plan and report only, without writing bundles (default: dry-run on)",
)
@click.option("--target-lead-hours", type=int, default=48, help="Adapt subscriptions anchored within this many hours (default: 48)")
@click.option("--min-lead-hours", type=int, default=24, help="Never edit inside this lock window before the anchor (default: 24)")
@click.option("--limit", type=int, default=None, help="Process at most N subscriptions (for ramp/testing)")
@click.option("--seed", type=int, default=None, help="Seed the random candidate picker for reproducible runs")
@click.option(
    "--output", "output_file",
    type=click.Path(dir_okay=False),
    default="menu_autofill_audit.csv",
    help="Audit CSV report path (default: menu_autofill_audit.csv)",
)
def autofill_menu(
    dry_run: bool,
    target_lead_hours: int,
    min_lead_hours: int,
    limit: Optional[int],
    seed: Optional[int],
    output_file: str,
) -> None:
    """Swap stale meals in upcoming Loop bundle subscriptions for in-menu ones."""
    asyncio.run(
        _run_autofill_menu(
            dry_run, target_lead_hours, min_lead_hours, limit, seed, output_file
        )
    )


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


async def _run_unpublish_inactive_meals(dry_run: bool) -> None:
    cfg = load_config()
    setup_logging(cfg.migration_log_file, cfg.log_level)

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
            renderer=DescriptionRenderer(cfg.description_template),
            django_dsn=cfg.django_database_url,
            django_media_url=cfg.django_media_url,
            locale=cfg.meal_locale,
            subscription_group_code=cfg.subscription_group_code,
        )

        await resource.unpublish_inactive_products()


async def _run_generate_vouchers(
    email_file: str,
    amount: float,
    days: int,
    prefix: str,
    column: str,
    purchase_type: str,
    recurring_cycles: int,
    output_file: str,
    dry_run: bool,
) -> None:
    cfg = load_config()
    setup_logging(cfg.migration_log_file, cfg.log_level)

    emails = read_emails(Path(email_file), column=column)
    if not emails:
        raise click.ClickException(f"No emails found in '{email_file}' (column '{column}').")

    applies_one_time = purchase_type in ("one-time", "both")
    applies_subscription = purchase_type in ("subscription", "both")

    if dry_run:
        console.print("[bold yellow]DRY RUN MODE — no writes will be made to destination[/]")
    sub_note = (
        f", subscriptions: {'every order' if recurring_cycles == 0 else f'{recurring_cycles} cycle(s)'}"
        if applies_subscription
        else ""
    )
    console.print(
        f"Generating vouchers for [cyan]{len(emails)}[/] emails: "
        f"-{amount:.2f} EUR, valid {days} days, applies to [cyan]{purchase_type}[/]{sub_note}"
    )

    async with _make_client(
        cfg, cfg.dest_shop, cfg.dest_token, cfg.dest_client_id, cfg.dest_client_secret
    ) as dest:
        generator = VoucherGenerator(
            dest_client=dest,
            amount=amount,
            days=days,
            prefix=prefix,
            applies_one_time=applies_one_time,
            applies_subscription=applies_subscription,
            recurring_cycles=recurring_cycles,
            dry_run=dry_run,
        )
        rows = await generator.run(emails)

    write_report(rows, Path(output_file))

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1

    table = Table(title="Voucher Generation Summary", show_lines=True)
    table.add_column("Status", style="cyan")
    table.add_column("Count", justify="right")
    for status_name in ("created", "exists", "would-create", "failed"):
        if status_name in counts:
            style = "red" if status_name == "failed" else "green"
            table.add_row(f"[{style}]{status_name}[/]", str(counts[status_name]))
    console.print(table)
    console.print(f"Report written to [cyan]{output_file}[/]")

    failed = counts.get("failed", 0)
    if failed:
        console.print(f"[red]{failed} email(s) failed — see the report for details.[/]")


async def _run_update_vouchers(
    report_file: str,
    amount: Optional[float],
    days: Optional[int],
    purchase_type: Optional[str],
    recurring_cycles: Optional[int],
    usage_limit: Optional[int],
    once_per_customer: Optional[bool],
    output_file: str,
    dry_run: bool,
) -> None:
    cfg = load_config()
    setup_logging(cfg.migration_log_file, cfg.log_level)

    if all(
        v is None
        for v in (amount, days, purchase_type, recurring_cycles, usage_limit, once_per_customer)
    ):
        raise click.ClickException(
            "Nothing to update — pass at least one of --amount, --days, --purchase-type, "
            "--recurring-cycles, --usage-limit, or --once-per-customer/--no-once-per-customer."
        )

    rows = read_report(Path(report_file))
    if not rows:
        raise click.ClickException(f"No rows found in report '{report_file}'.")

    if dry_run:
        console.print("[bold yellow]DRY RUN MODE — no writes will be made to destination[/]")

    changes: list[str] = []
    if amount is not None:
        changes.append(f"amount=-{amount:.2f} EUR")
    if days is not None:
        changes.append(f"expiry=now+{days}d")
    if purchase_type is not None:
        changes.append(f"applies to {purchase_type}")
    if recurring_cycles is not None:
        changes.append(f"recurring cycles={recurring_cycles}")
    if usage_limit is not None:
        changes.append(f"usage limit={usage_limit}")
    if once_per_customer is not None:
        changes.append(f"once per customer={once_per_customer}")
    console.print(
        f"Updating [cyan]{len(rows)}[/] vouchers from '{report_file}': "
        + ", ".join(changes)
    )

    async with _make_client(
        cfg, cfg.dest_shop, cfg.dest_token, cfg.dest_client_id, cfg.dest_client_secret
    ) as dest:
        updater = VoucherUpdater(
            dest_client=dest,
            amount=amount,
            days=days,
            purchase_type=purchase_type,
            recurring_cycles=recurring_cycles,
            usage_limit=usage_limit,
            once_per_customer=once_per_customer,
            dry_run=dry_run,
        )
        results = await updater.run(rows)

    write_report(results, Path(output_file), fields=_UPDATE_REPORT_FIELDS)

    counts: dict[str, int] = {}
    for row in results:
        counts[row["status"]] = counts.get(row["status"], 0) + 1

    table = Table(title="Voucher Update Summary", show_lines=True)
    table.add_column("Status", style="cyan")
    table.add_column("Count", justify="right")
    for status_name in ("updated", "would-update", "not-found", "skipped", "failed"):
        if status_name in counts:
            style = "red" if status_name in ("failed", "not-found") else "green"
            table.add_row(f"[{style}]{status_name}[/]", str(counts[status_name]))
    console.print(table)
    console.print(f"Report written to [cyan]{output_file}[/]")

    failed = counts.get("failed", 0)
    if failed:
        console.print(f"[red]{failed} voucher(s) failed — see the report for details.[/]")


async def _run_autofill_menu(
    dry_run: bool,
    target_lead_hours: int,
    min_lead_hours: int,
    limit: Optional[int],
    seed: Optional[int],
    output_file: str,
) -> None:
    import random

    cfg = load_config()
    setup_logging(cfg.migration_log_file, cfg.log_level)

    if not cfg.loop_admin_token:
        raise click.ClickException(
            "LOOP_ADMIN_TOKEN is not set. Add it to .env (scopes: read subscription "
            "contracts, read & write customers)."
        )

    if dry_run:
        console.print("[bold yellow]DRY RUN MODE — no bundles will be written[/]")

    rng = random.Random(seed)
    admin = LoopAdminClient(
        cfg.loop_admin_token,
        base_url=cfg.loop_api_base_url,
        api_version=cfg.loop_api_version,
        max_retries=cfg.max_retries,
    )
    storefront = LoopStorefrontClient(
        admin,
        base_url=cfg.loop_api_base_url,
        api_version=cfg.loop_api_version,
        max_retries=cfg.max_retries,
    )

    async with (
        _make_client(cfg, cfg.dest_shop, cfg.dest_token, cfg.dest_client_id, cfg.dest_client_secret) as dest,
        admin,
        storefront,
    ):
        autofiller = MenuAutofiller(
            shopify=dest,
            admin=admin,
            storefront=storefront,
            active_menu_tag=cfg.active_menu_tag,
            target_lead_hours=target_lead_hours,
            min_lead_hours=min_lead_hours,
            dry_run=dry_run,
            rng=rng,
        )
        rows = await autofiller.run(limit=limit)

    write_audit(rows, Path(output_file))

    counts: dict[str, int] = {}
    for row in rows:
        counts[row.decision or "?"] = counts.get(row.decision or "?", 0) + 1

    table = Table(title="Menu Auto-Fill Summary", show_lines=True)
    table.add_column("Decision", style="cyan")
    table.add_column("Count", justify="right")
    for decision_name in ("skip", "adapt", "flag", "locked", "no_bundle", "error"):
        if decision_name in counts:
            style = "red" if decision_name in ("flag", "error") else "green"
            table.add_row(f"[{style}]{decision_name}[/]", str(counts[decision_name]))
    console.print(table)
    console.print(f"Audit report written to [cyan]{output_file}[/]")

    failed = sum(1 for r in rows if not r.ok)
    if failed:
        console.print(f"[red]{failed} subscription(s) need attention — see the audit report.[/]")


if __name__ == "__main__":
    cli()

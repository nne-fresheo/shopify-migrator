# Shopify Store Migration Tool

One-time migration tool that copies store data from a live Shopify source store to a new empty destination store. Migrates products, collections, inventory, pages, blogs, articles, URL redirects, price rules, discount codes, gift cards, selling plan groups, navigation menus, and standalone files.

**Out of scope:** orders, customers, theme files, PageFly content, Judge.me reviews.

---

## Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/) — used for dependency management and running the tool
- A Shopify private app (or custom app) access token for both the source and destination stores with the scopes listed below

---

## Setup

**1. Clone the repository and install dependencies**

```bash
git clone <repo-url>
cd shopify-migrator
uv sync
```

**2. Create your `.env` file**

```bash
cp .env.example .env
```

Edit `.env` and fill in the required values. Two authentication methods are supported:

**Option A — Static access token** (custom app token from the Shopify admin):

```env
SOURCE_SHOP_DOMAIN=my-source-store.myshopify.com
SOURCE_ACCESS_TOKEN=shpat_xxxxxxxxxxxxxxxxxxxxxxxx

DEST_SHOP_DOMAIN=my-dest-store.myshopify.com
DEST_ACCESS_TOKEN=shpat_yyyyyyyyyyyyyyyyyyyyyyyy
```

**Option B — OAuth client credentials** (client ID + secret from the Partners dashboard). A 24-hour token is fetched automatically on startup. If both are set, client credentials take priority.

```env
SOURCE_SHOP_DOMAIN=my-source-store.myshopify.com
SOURCE_CLIENT_ID=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
SOURCE_CLIENT_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

DEST_SHOP_DOMAIN=my-dest-store.myshopify.com
DEST_CLIENT_ID=yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy
DEST_CLIENT_SECRET=yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy
```

**Required Shopify API scopes**

| Store | Scopes |
|-------|--------|
| Source | `read_products`, `read_inventory`, `read_collections`, `read_content`, `read_price_rules`, `read_discounts`, `read_gift_cards`, `read_files`, `read_themes`, `read_selling_plans` |
| Destination | `write_products`, `write_inventory`, `write_collections`, `write_content`, `write_price_rules`, `write_discounts`, `write_gift_cards`, `write_files`, `write_themes`, `write_selling_plans`, `read_metaobjects` |

> `read_metaobjects` is required on the destination for the native
> `shopify.allergen-information` / `shopify.dietary-preferences` metafields:
> the sync resolves each meal's allergen/diet flags to standard metaobject GIDs
> before writing the reference. Without it those native metafields are skipped
> (logged as a warning) while the `fresheo.*` metafields still sync.

**Product metafield definitions (destination)**

`sync-meals` writes metafield *values*; the definitions must exist on the
destination's **Product** owner type for them to render (Settings → Custom data
→ Products). Create the custom `fresheo.*` ones; add the standard `shopify.*`
ones from Shopify's standard templates.

| Namespace / key | Type | Source |
|-----------------|------|--------|
| `fresheo.nutri_score` | Single line text | Nutri-Score letter (A–E) |
| `fresheo.nutrition` | JSON | Macros (kcal, protein, carbs, …) |
| `fresheo.cooking_instructions` | JSON | `{cold, microwave, oven}` |
| `fresheo.author` | Single line text | Chef name (sparse) |
| `fresheo.diet` | List of single line text | Active diet slugs (filter_string, falling back to recipe flags; resolution-free) |
| `fresheo.image_sources` | List of URLs | Internal bookkeeping for image diffing |
| `shopify.dietary-preferences` | Dietary preferences (standard) | Diet flags → standard metaobjects |
| `shopify.allergen-information` | Allergen information (standard) | Recipe allergens → standard metaobjects |

> `fresheo.diet` is a plain-text mirror of the diet signal that needs no
> metaobject resolution or `read_metaobjects` scope, so it always populates —
> use it as a fallback for theming when the native `shopify.dietary-preferences`
> reference isn't available.

---

## Running the Tool

All commands are run via `uv run main.py <command>`.

### Full migration (extract + load)

```bash
uv run main.py migrate
```

### Step-by-step

**Phase 1 — Extract** (reads from source store, writes to `data/`)

```bash
uv run main.py extract
```

**Phase 2 — Load** (reads from `data/`, writes to destination store)

```bash
uv run main.py load
```

### Dry run (validate without writing to destination)

```bash
uv run main.py load --dry-run
# or
uv run main.py migrate --dry-run
```

### Check progress

```bash
uv run main.py status
```

### Operate on a single resource

Any command accepts `--resource <name>` to limit scope:

```bash
uv run main.py extract --resource products
uv run main.py load --resource collections
```

Valid resource names (must be run in this order if loading individually):
`files`, `products`, `collections`, `inventory`, `pages`, `blogs`, `articles`, `redirects`, `price_rules`, `discount_codes`, `gift_cards`, `selling_plans`, `menus`

### Force re-extract or re-create

```bash
# Re-extract even if data/products.json already exists
uv run main.py extract --force

# Re-create resources even if already mapped in a previous run
uv run main.py load --force
```

### Post-processing: rewrite embedded image URLs

After files and content (pages, articles) are fully loaded, run this to replace source CDN URLs in HTML bodies with destination CDN URLs:

```bash
uv run main.py rewrite-images
```

### Weekly menu auto-fill for Loop subscriptions

Each week the active menu rotates (Shopify products tagged `current-menu`). Subscribers who don't re-pick their meals would have stale, out-of-menu dishes in their next box. Run this **daily** to swap each stale meal in a subscription's next upcoming Loop bundle for an active meal of the **same category** (the bare category tag — `main-dish`, `dessert`, …) and **same quantity**:

```bash
# Dry run (default): plan + write menu_autofill_audit.csv, no bundle writes
uv run main.py autofill-menu

# Live (requires the Loop Storefront Bundle beta enabled on the account)
uv run main.py autofill-menu --no-dry-run --seed 7
```

Behavior:

- A bundle whose meals are all in-menu is **skipped**; idempotent, safe to re-run daily.
- A stale meal with no same-category replacement **flags the whole subscription** for manual review and is **not written** (no cross-category fallback).
- Subscriptions inside `--min-lead-hours` (default 24) of their anchor are **locked** and never edited; `--target-lead-hours` (default 48) bounds discovery.
- Every subscription produces an audit row (decision, removed/added variants, category mapping) in the `--output` CSV.

Requires `LOOP_ADMIN_TOKEN` (and the Loop beta for the write path) — see `.env.example`. The read-only dry run uses only the Shopify and Loop Admin APIs.

---

## Resumability

- **Extract phase:** if `data/<resource>.json` already exists it is reused. Delete the file or use `--force` to re-fetch.
- **Load phase:** `logs/progress.json` tracks every loaded resource. Re-running `load` picks up from where it left off — already-completed resources are skipped automatically.

---

## Output Files

| Path | Description |
|------|-------------|
| `data/*.json` | Extracted resource data from source store |
| `data/id_maps/*.json` | Source ID → destination ID mappings, written incrementally during load |
| `logs/migration.log` | Full debug log (all levels) |
| `logs/progress.json` | Per-resource load progress |
| `logs/failed_resources.json` | Resources that failed to create, with payloads and error messages |

---

## Location Mapping

Inventory levels require matching source and destination locations by name. If location names differ between stores, create a `location_map.json` file in the project root:

```json
{
  "source_location_id": "dest_location_id"
}
```

---

## Known Limitations

- **Gift card balances:** Cards with partial redemptions are created with their full `initial_value` (Shopify does not allow setting `balance` directly).
- **Selling plan subscriptions:** Only plan group configuration is migrated. Active subscriber contracts are not transferred.
- **Embedded HTML images:** `<img>` tags in page/article bodies continue referencing the source CDN until `rewrite-images` is run.
- **PageFly, Judge.me, Microsoft Clarity:** Must be migrated manually via their respective admin panels. See [MIGRATION_SPEC.md](MIGRATION_SPEC.md) § 11 for step-by-step instructions.
- **App metafields:** Metafields namespaced to third-party apps are not migrated.

---

## Running Tests

```bash
uv run python -m pytest
```

With coverage:

```bash
uv run python -m pytest --cov=migration --cov-report=term-missing
```

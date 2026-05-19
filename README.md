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
| Destination | `write_products`, `write_inventory`, `write_collections`, `write_content`, `write_price_rules`, `write_discounts`, `write_gift_cards`, `write_files`, `write_themes`, `write_selling_plans` |

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

# Job Specification — Weekly Menu Auto‑Fill for Loop Bundle Subscriptions

**Status:** Ready for implementation in Claude Code
**Owner:** Nathan Nepper
**Last updated:** 2026-06-20

---

## 1. Goal

Every week the active menu (the set of meals a subscriber may choose from) rotates. Subscribers are expected to log in and pick their meals; many won't. This job runs **daily** and, for every subscription whose **next upcoming order** still contains meals that are **not in the current menu**, swaps each stale meal for an active meal of the **same category and same quantity**, so the order generated on the anchor date is always coherent and in‑stock.

### Core rule (locked)

> For each subscription, look at the **next upcoming order's** bundle contents.
> - If **every** meal is in the current menu → **skip**.
> - If **any** meal is not in the current menu → **adapt**: for each stale meal, swap it for a meal of the **same category** with the **same quantity**, then write the updated bundle.

State‑based ⇒ idempotent and safe to re‑run daily.

### Confirmed decisions (all resolved)
- **Subscriptions are BYOB bundles** → write **only** via the Loop **Storefront bundle update API**.
- **Active menu** = Shopify products carrying the tag **`current-menu`** (re‑applied weekly). Not a fixed collection id.
- **Category** = a **Shopify product tag with a prefix**. The exact prefix and value vocabulary are **to be inferred from the existing codebase** by the implementing agent (parameter `category_tag_prefix`).
- **Swap rule** = same category, same quantity. Already‑valid meals are left untouched; only stale meals are replaced.
- **No same‑category candidate** → substitute from a configured **default category** so the box is always fully in‑menu.
- **Order cutoff** = Loop generates the upcoming order within **~24 h** of the anchor ⇒ `min_lead_hours = 24` (never edit inside this window).
- **Customer notification** = **none** (silent adapt; internal audit log only).
- **Storefront bundle API** = **beta, not yet enabled** → enabling it on the Loop account is the one remaining external prerequisite (Section 11). The per‑customer token flow itself uses standard endpoints (Section 2.1).

---

## 2. Systems & access

| System | Used for | Auth |
|---|---|---|
| **Loop Admin API** (`/admin`) | Discover subscriptions with an upcoming anchor; read contents + `bundleTransactionId`; mint customer session tokens | `X-Loop-Token` header (Admin token). Scopes: *Read subscription contracts*, *Read & write customers* |
| **Loop Storefront Bundle API** (`/storefront`) | Read bundle contents + box size/discount; **write** updated bundle; exchange session→access token | Bearer **access token**, per customer (Section 2.1). **Beta — must be enabled by Loop** |
| **Shopify Admin API** | List products tagged `current-menu` (+ variants + tags) to build the active menu and read category tags | Shopify Admin access token / private app |

### 2.1 Minting the per‑customer storefront access token (admin‑initiated)

The bundle write is a Storefront API and needs a **customer** bearer token. A backend job can mint one for any customer using only the Admin token:

1. **Admin** → `POST /admin/2023-10/customer/{customerShopifyId}/sessionToken` (`X-Loop-Token`) → `sessionToken`. *(Scope: Read & write customers. `customerShopifyId` comes from the scheduled‑orders / subscription read.)*
2. **Storefront** → `POST /storefront/2023-10/auth/refreshToken` with `{ "sessionToken": "..." }` → `accessToken` (≈4‑day) + `refreshToken` (≈30‑day).
3. Use `accessToken` as `Authorization: Bearer ...` for `bundle/transaction/*` calls.
4. Cache tokens per customer; when `accessToken` expires, rotate via **Rotate access token** (`/storefront/.../rotate`) using the `refreshToken` instead of re‑minting.

> This means no customer interaction is required — the job is fully back‑end. The only blocker is Loop enabling the beta bundle endpoints for your account.

---

## 3. Data model & how the pieces connect

From **Read subscription details** (`GET /admin/2023-10/subscription/{id}`), each meal line carries `variantShopifyId`, `productShopifyId`, `quantity`, `bundleTransactionId` (= `_bundleId`), and `isOneTimeAdded`/`isOneTimeRemoved` (one‑off changes scoped to the next order).

From the bundle (`GET /storefront/2023-10/bundle/transaction/{transactionId}` and `GET /storefront/2023-10/bundle/{bundleId}`): authoritative current `items`, `boxSizeId`, eligible `discountId` — all three required to re‑submit an update without breaking box size/discount.

The active menu + categories come from **Shopify**: products tagged `current-menu`, each product's tags giving its category (`category_tag_prefix`).

---

## 4. Inputs & configuration

```yaml
# Secrets / connection
loop_admin_token:        <secret>   # scopes: read subscription contracts, read & write customers
shopify_admin_token:     <secret>
shopify_store_domain:    your-store.myshopify.com

# Active menu (Shopify product tag)
active_menu_tag:         "current-menu"

# Category (swap key) — Shopify product tag prefix
category_tag_prefix:     "<INFER FROM CODEBASE>"   # e.g. "cat:"; agent to confirm from existing repo
default_category:        "<INFER/CONFIRM>"          # fallback category when no same-category match

# Timing
run_cadence:             "daily"
target_lead_hours:       48     # aim to adapt this far before the anchor
min_lead_hours:          24     # order generates within ~24h; never edit inside this window

# Safety
notify_customer:         false  # silent adapt; internal audit log only
dry_run:                 true
random_seed:             null   # set for reproducible candidate picks in tests
```

---

## 5. Algorithm

### Step 0 — Build the active menu (once per run)
From Shopify, list all products with tag `active_menu_tag` (`current-menu`), including variants and tags. Build:
- `activeVariantIds: Set<int>` — variants sellable this week (exclude out‑of‑stock),
- `mealsByCategory: Map<category, Meal[]>` — active meals grouped by their `category_tag_prefix` tag; each `{ productShopifyId, variantShopifyId, title, category, inStock }`,
- `variantToCategory: Map<variantId, category>` for quick lookup.

### Step 1 — Discover subscriptions with an upcoming anchor
**Read all scheduled orders** (`GET /admin/2023-10/order/schedule/`): `billingDateStartEpoch = now`, `billingDateEndEpoch = now + target_lead_hours`; paginate on `pageInfo.hasNextPage`; keep `status = UNPROCESSED`. Each row → `subscription.id`, `customer.shopifyId`, `billingDateEpoch`. Rate limit **2 req / 3 s**.
**Skip anything inside `min_lead_hours`** of its anchor (order may be locked) → flag for review.

### Step 2 — Read the next upcoming order's bundle contents
1. `GET /admin/2023-10/subscription/{id}` → meal lines + `bundleTransactionId` (confirm non‑null = bundle).
2. Mint/lookup the customer's storefront `accessToken` (Section 2.1).
3. `GET /storefront/2023-10/bundle/transaction/{bundleTransactionId}` → current `items`, `boxSizeId`, `discountId`.
4. Compute **effective next‑order meals** = items adjusted by any `isOneTimeAdded`/`isOneTimeRemoved`, so we judge what actually ships.

### Step 3 — Decide skip vs adapt
```
stale = [ m for m in effectiveMeals if m.variantShopifyId not in activeVariantIds ]
SKIP if stale is empty else ADAPT
```

### Step 4 — Build the new bundle items (same‑category, same‑quantity)
Start from current `items`. For each `stale` meal:
1. Category = its `category_tag_prefix` tag (from Shopify; fall back to last‑known if the product is gone).
2. Pick a replacement **at random** from `mealsByCategory[category]`, excluding meals already in the box and out‑of‑stock.
3. If that category has no candidate → pick from `mealsByCategory[default_category]` (same exclusions). Record the fallback.
4. Replace the stale entry with `{ productVariantShopifyId, quantity: <same> }`.
5. Keep already‑valid meals unchanged.

Keep total item count identical so `boxSizeId` stays valid; re‑select `discountId` if box‑quantity thresholds changed (from `GET /storefront/.../bundle/{bundleId}`).

### Step 5 — Write via the bundle update API
`POST /storefront/2023-10/bundle/transaction/update` (Bearer access token):
```json
{
  "id": "<bundleTransactionId>",
  "items": [ { "productVariantShopifyId": <id>, "quantity": <q> }, ... ],
  "discountId": "<eligible discount id>",
  "boxSizeId": "<box size id>"
}
```
`items` is the **full** replacement list. Skip the write entirely when `dry_run`.

### Step 6 — Verify & record
1. Re‑read the bundle/subscription and **assert** the effective next‑order meals ⊆ `activeVariantIds`, **and that the change reflects on the next upcoming order** (Section 7).
2. Audit row: `subscriptionId, customerShopifyId, billingDateEpoch, decision, removed[], added[], categoryMapping, usedFallback, dryRun, ts, ok/err`.
3. On error/failed assertion → dead‑letter + alert; no blind retry.

---

## 6. Endpoint reference

| Purpose | Method & path | Pool / limit |
|---|---|---|
| Find subscriptions with an upcoming anchor | `GET /admin/2023-10/order/schedule/` (`billingDate*Epoch`, paginated) | 2 req / 3 s |
| Read subscription contents + `bundleTransactionId` | `GET /admin/2023-10/subscription/{id}` | 10 req/s |
| Mint customer session token | `POST /admin/2023-10/customer/{customerShopifyId}/sessionToken` | 10 req/s |
| Exchange session → access/refresh token | `POST /storefront/2023-10/auth/refreshToken` | — |
| Rotate access token | `POST /storefront/2023-10/auth/rotate…` (refresh token) | — |
| Read current bundle contents (items, boxSize, discount) | `GET /storefront/2023-10/bundle/transaction/{transactionId}` | — |
| Read bundle config (box sizes, discounts, limits) | `GET /storefront/2023-10/bundle/{bundleId}` | — |
| **Write** updated bundle | `POST /storefront/2023-10/bundle/transaction/update` | — |
| List `current-menu` products + tags | Shopify Admin API | Shopify limits |

Pagination: `pageNo` (from 1), `pageSize` (max 50); loop on `pageInfo.hasNextPage`.

---

## 7. Next‑order propagation — the one risk to validate

Loop's bundle update is documented to affect "future renewals," which does **not** explicitly guarantee the **immediate** next order. Treat this as a **go/no‑go test**, not an assumption:

1. **Run daily with lead** (`target_lead_hours ≈ 48`) and **never edit inside `min_lead_hours = 24`** of the anchor.
2. **Propagation test during the spike:** on a test subscription, update the bundle, then re‑read subscription details **and** the scheduled order; confirm the upcoming order's meals changed *before* its anchor.
   - **Reflects** → bundle‑update‑only is sufficient. Ship as designed.
   - **Renewal‑only** → escalate to Loop for the supported way to apply the change to the immediate next order; make that a required dependency before go‑live.
3. **Always assert post‑write** that the *upcoming* order (not a later one) is now valid; alert on mismatches.

---

## 8. Scheduling & throughput

- **Daily run** with `billingDateEndEpoch = now + target_lead_hours` catches each subscription once, in‑window, on any weekday.
- **Idempotent:** after a successful adapt, meals are ⊆ menu so the next run skips. Persist a `(subscriptionId, billingCycle)` processed‑marker anyway.
- **Rate budget:** discovery (2 req/3 s) is the bottleneck. Separate limiters per pool (global 10/s; schedule 2/3 s), jitter, capped concurrency. Global pool is **per store**, shared with other integrations — leave headroom. Token minting adds 1 admin + 1 storefront call per new‑token customer; cache aggressively.

---

## 9. Edge cases & risks

1. **Propagation to the next order** — central risk; gate go‑live on the Section 7 test.
2. **Recurring staple meals** in both weeks count as valid and aren't swapped. Accepted.
3. **Partial validity** — only stale meals swapped; valid ones preserved.
4. **Category missing on a product** — fall back to last‑known, then `default_category`; log.
5. **No same‑category candidate** — substitute from `default_category` (never ship short); record fallback.
6. **Box size / discount integrity** — keep item count constant; re‑select `discountId`/`boxSizeId`.
7. **Out‑of‑stock** active variants excluded from candidates.
8. **One‑time next‑order edits** folded into the effective‑order evaluation (Step 2.4).
9. **Cutoff / locked orders** — skip + flag anything inside `min_lead_hours`.
10. **Concurrent customer pick** — read immediately before write, re‑validate; idempotency makes re‑runs harmless.
11. **Token expiry / rotation** — handle 401s by rotating via refresh token, then one retry.
12. **Prepaid subscriptions** (`isPrepaid: true`) — confirm whether in scope (assume yes unless told otherwise).
13. **Time zones** — anchors are epoch UTC; align lead/cutoff to the merchant's local cutoff.

---

## 10. Build plan for Claude Code

1. **Infer config from the repo:** the exact `category_tag_prefix` and a sensible `default_category` (confirm with a maintainer).
2. **Read‑only spike (`dry_run=true`):** Steps 0–4. Output a per‑subscription report: due list, skip/adapt, proposed swaps (stale → replacement + category, fallbacks flagged). No writes, no token minting required beyond reads.
3. **Token minting:** implement Section 2.1 with per‑customer caching + rotation.
4. **Propagation test (go/no‑go):** Section 7.2 on a test subscription.
5. **Writer:** `bundle/transaction/update` honoring `dry_run`, box‑size/discount integrity.
6. **Verify + audit log + dead‑letter** (Step 6).
7. **Rate‑limit layer:** per‑pool token buckets + jitter.
8. **Schedule** the daily run; metrics (due / skipped / adapted / fallback / failed) + alerting.
9. **Ramp:** `dry_run=false` on a small cohort, then scale.

---

## 11. Remaining external prerequisites (not blockers for the dry‑run)

- **Enable the beta Storefront Bundle API** on the Loop account (contact your account success manager). Needed only for the write path and the live propagation test; Steps 0–4 (read‑only) don't require it.
- **Confirm `category_tag_prefix` + `default_category`** from the codebase/maintainer.
- **Validate next‑order propagation** (Section 7) — decides whether a next‑order fallback dependency is needed.
- **Confirm prepaid subscriptions** are in scope.

---

*Prepared from the Loop Developer Hub API reference (Admin + Storefront APIs, 2023‑10). Endpoint shapes and the admin→storefront token flow verified against the published OpenAPI definitions.*

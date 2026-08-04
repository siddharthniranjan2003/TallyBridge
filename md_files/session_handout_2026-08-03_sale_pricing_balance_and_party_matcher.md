# TallyBridge — Sale Pricing, Voucher Balance & the Party Matcher: Session Handout

Context-transfer doc for a new chat. Covers 2026-08-02 → 2026-08-03. Four workstreams;
three shipped, one **half-finished and needing action**.

Picks up after `session_handout_2026-07-31_sale_rate_latest_global.md`.

---

## 0. TL;DR — read this first

| # | Workstream | State |
|---|---|---|
| 1 | **Sale pricing rule** (global-latest rate + party-scoped discount) | ✅ live on testing + client |
| 2 | **Voucher balance fix** (Tally rejected ~50% of edited invoices) | ✅ live on testing + client |
| 3 | **App: edit/add pricing + Purchase/Report hidden** | ⚠️ committed & pushed, **never deployed** |
| 4 | **Party matcher — DISTINCT RPC** | 🚧 **IN PROGRESS — SQL not yet applied** |

**Immediate next action:** paste `supabase/migrations/20260803_distinct_party_names.sql` into
the **testing** (`yynuu`) SQL Editor, then re-run the verification in §4.

⚠️ **`parsing/server/handler.py` has uncommitted changes** for workstream 4.

---

## 1. Sale pricing rule — DONE

**The rule** (replaces the old two-tier waterfall):

```
rate     = latest GST SALE of the item to ANYONE   (party irrelevant)
discount = latest GST SALE of the item to THIS party; none -> 0%
```

Rate and discount are now independent lookups and **routinely come from different vouchers**.
That is intended: rate tracks the item's current market price, discount tracks this
customer's terms.

Implemented as two batch RPCs (`get_latest_sale_rates_for_items`,
`get_latest_party_discounts_for_items`) called by **both** the parsing service and the Flutter
app, so the rule lives in SQL and the two sides cannot drift.

**Verified end-to-end to Tally.** On a MOHIT SALES invoice: `FLAT FILE 150 SMOOTH JK` posted
at 417.00 @ 53% — rate borrowed from another customer, discount MOHIT's own.

### The consequence you accepted

**70% of the client's sale history carries a discount, median 53%.** Under the old rule a
customer with no history for an item inherited the borrowed sale's discount; now they get 0%.
Measured on one real invoice: **+111.8%** (₹96,869 → ₹205,136), with 3 lines accounting for
98.5% of the swing. Confirmed twice as intended.

### Known gap, assessed and accepted

**Changing the vendor does not re-price existing lines.** `resolveSalePricing` is only called
from the item picker. After a vendor swap, untouched lines keep the previous party's discount
while rates stay correct — so the invoice *looks* right. Verified live: swapping A.V. UNIPACK
→ GAURAV, `HSS T/S DRILL 40.5 MIRANDA` is ₹7,151 with A.V.'s 72% vs ₹25,541 without.

You judged this acceptable ("if vendor is changed the client changes every item anyway").
Counter-evidence noted for the record: the likeliest reason to change a vendor is a *misread
name with correct items*, where nothing prompts a re-pick.

---

## 2. Voucher balance fix — DONE

### The bug

`Not Pushed to Tally — Tally reported an import exception without a LINEERROR message.`
Tally returned `EXCEPTIONS=1, CREATED=0` and no reason.

**The voucher didn't balance after rounding.** The app's edit path stores charges at full
float precision, and `tally_pusher._format_amount` quantizes every line to 2dp
**independently**:

```
90790.70 + 8171.17 + 8171.17 = 107133.04
party 107133.0326678024      -> 107133.03      one paisa short -> rejected
```

**~50% of edited vouchers** land on an imbalance (simulated over 20k). Proven both ways: hand-
fixing the four ledger amounts made the identical voucher push; after the fix the same row
pushes with its stored amounts still unbalanced.

### The fix

`balancePushVoucherAmounts()` in `backend/src/routes/sync.ts`, applied in the
`GET /push-queue` **read path**. Rounds each item first, sets the inventory ledger to the
summed items, rounds the other credits, derives the party line from those rounded values.

**Why there:** the app writes `push_queue` **directly** and never passes through
`normalizePushVoucherPayload`, so the insert-side normalizer can't see app edits. Everything —
app, n8n, parsing service — funnels through `GET /push-queue` on the way to the poller.

**Known limitation:** the stored row keeps raw values, so the app can display a total one
paisa below what posts. Fixing that needs app-side rounding in `_recalcAmount`, `_addItem`,
`onStockItemSelected`, `_recomputeChargesFromItems` — deliberately not done.

---

## 3. App changes — COMMITTED, NOT DEPLOYED

Branch **`update/edit_and_add_rate-disc_ui_updated`** on `AiAccountant`, pushed, 7 commits.

- `lib/data/sale_pricing.dart` (new) — `resolveSalePricing` + `mergeSalePricing`
- `StockItem.discountSource` (new field)
- `voucher_detail_sheet.dart` — picker calls the RPCs; `_fetchLatestSaleRow` and
  `_fetchFallbackItem` **deleted** (both carried the `created_at` ordering bug fixed
  backend-side in `53014c9`); party list batch-repriced; `source_payload` write-back
- **Purchase and Report hidden** (client-requested), camera opens straight to Sale
- `test/sale_pricing_test.dart` — 13 tests

**`flutter build web` has never been run.** `analyze` and `test` pass (36 tests, 1
pre-existing `widget_test.dart` failure), but a passing linter is not evidence the build
compiles. **Run the build before deploying.**

### Three defects I introduced and fixed during the session

1. Picker label keyed off `rate_source == different_party`, which is now the *normal* case —
   moved to `discount_source == none`.
2. `_rebuiltSourcePayload` read `_editableItems`, which **Revert clears before persisting** —
   wiped provenance for 20 lines on a testing row. Now reads the payload being persisted.
3. `Map.from` is shallow, so `_stripItemLocalKeys` deleted `__rate_source` **before** the
   rebuild read it. Reordered.

Two of the three were silent — they wrote wrong data without erroring.

---

## 4. Party matcher — 🚧 IN PROGRESS, ACTION REQUIRED

### What happened

Two scans became "Garbage invoice" on the client (`scan_jobs`, both
`No party_name matched in Supabase`). They have **different causes**.

| invoice | job_id | cause |
|---|---|---|
| `MUNDHRA AGENCIES` | `b5635afc…` | **retrieval** — customer never a candidate |
| `P. T ENT.` | `ca639a9e…` | **scoring** — initials discarded |

### `MUNDHRA AGENCIES` — the retrieval bug (being fixed)

I refuted three of my own hypotheses (header crop, garbled read, scoring cliff) before
replaying the real image through the live testing service:

```
ocr_text   : 'MUNDHRA AGENCIES'      <- read perfectly
matched_name: ''
candidates : BALAJI H/W AGENCIES · AMBEY TOOLS · ASCENT INDUSTRY ...   <- all A/B
```

`fetch_supabase_party_candidates` pages the **`vouchers`** table (6 × 1000 rows) for
`party_name`. `vouchers` has one row per **invoice**, so 6,000 rows is 6,000 invoices and the
busiest early-alphabet customers exhaust it:

```
6000 rows fetched -> 211 distinct customers, reaching '3S DESIGN' .. 'Cash'
1165 of 1376 customers invisible
```

The targeted `ILIKE '*TERM*'` rescue missed too — the ledger says **MUNDHA-RA**, the challan
says **MUNDH-RA**, and a transposition defeats a substring match:

```
ILIKE *MUNDHRA*  -> 0 rows        ILIKE *MUNDHARA* -> MUNDHARA AGENCIES
```

Exact matching upstream of a fuzzy matcher — the fuzzy stage never got the chance.

### What is already written (uncommitted)

- `supabase/migrations/20260803_distinct_party_names.sql` + `_rollback.sql` — **new, untracked**
- `parsing/server/handler.py` — **modified, uncommitted**: new `fetch_distinct_party_names()`,
  paging loop moved verbatim into `_party_candidates_by_paging()` as a fallback

**The fallback is load-bearing.** A project without the migration returns `PGRST202`; if that
wiped the candidate list, *every* scan would become a garbage invoice. Safe to deploy with or
without the SQL.

**Verified locally:** with the RPC absent, the fallback reproduces the bug exactly — 211
candidates, best match `BALAJI H/W AGENCIES` at 31.85, rejected. Identical to the live service.

### ▶ NEXT STEPS

1. Paste `20260803_distinct_party_names.sql` into the **testing** (`yynuu`) SQL Editor.
2. Re-run:
   ```bash
   cd parsing && python3 -c "
   import sys; sys.path.insert(0,'.')
   from server import handler as h
   h.PARTY_NAME_CACHE=None
   c=h.fetch_supabase_party_candidates('MUNDHRA AGENCIES')
   print(len(c), 'MUNDHARA AGENCIES' in c)
   print(h.rank_party_candidates('MUNDHRA AGENCIES',c)[0])"
   ```
   Expect **~1376**, `True`, score **~122.87** (was 211 / False / 31.85).
3. Commit + deploy parsing to testing.
4. Replay the saved image — **no `&push=queue`**, nothing gets enqueued:
   ```bash
   curl -X POST 'https://tallybridge-parsing-828647628834.asia-south1.run.app/?type=sale' \
     -H 'Content-Type: image/jpeg' --data-binary @fail1.jpg
   ```
   `party_name_match.matched_name` should now be `MUNDHARA AGENCIES`.
5. Then apply SQL + deploy to client.

### `P. T ENT.` — SEPARATE, NOT FIXED

That customer **was** fetched and **was** scored, then rejected at **54.56** (threshold 78)
despite 90% string similarity. `strong_party_tokens` drops tokens under 3 characters, so
`P` and `T` vanish and only the generic `ENTERPRISES` remains — nothing identifying survives.

**63 of the client's 1,490 customers are named this way** (`A K TOOLS`, `S R ENTERPRISES`,
`OM TRADERS`, `MK TRADERS`, `P. T. ENTERPRISES`…).

Two things measured, both refuting my first design:

- **Adding `SALES` to `PARTY_GENERIC_TOKENS` changes nothing.** Both penalties are gated on
  `strong_tokens` being non-empty (`:444-445`), so a probe of purely generic words gets *no*
  penalty and raw fuzzy still clears 78. The matcher is most permissive when it knows least.
- **A "require a strong token" guard would reject those 63 customers**, including the one it
  was meant to fix.

**Validated fix (harness-measured, not yet applied):** abbreviation expansion in
`normalize_party_name`, reusing the existing `HARDW -> HARDWARE` idiom — add
`ENT`/`ENTP`/`ENTR` → `ENTERPRISES`, `AGY` → `AGENCY`, `CORP` → `CORPORATION`.

| | self-match | abbreviated read | `P.T ENT.` |
|---|---|---|---|
| baseline | 1489/1490 | 1459/1490 | ✗ 54.56 |
| with expansion | **1489/1490** | **1488/1490** | ✅ 86.0 |

+29 parties, zero regression. The deeper fix — stop discarding initials — was **not** measured.

---

## 5. Deployment state

| component | testing | client |
|---|---|---|
| Supabase: 2 pricing RPCs | ✅ applied | ✅ applied |
| Supabase: `get_distinct_party_names` | ❌ **pending** | ❌ pending |
| `tallybridge-parsing` | `00018-nk6` ✅ | `00012-2lx` ✅ |
| `tallybridge-backend` (balance fix) | `00013-2wj` ✅ | `00005-g4z` ✅ |
| Flutter app | ❌ **never deployed** | ❌ never deployed |

All four Cloud Run services were verified by downloading their build archives and diffing
against HEAD — identical. **The app is the only undeployed component.**

### Repo state

| repo | branch | state |
|---|---|---|
| TallyBridge | `update/absolute-latest-rate-etc` | in sync; **`parsing/server/handler.py` uncommitted** + 2 new migration files |
| AiAccountant | `update/edit_and_add_rate-disc_ui_updated` | in sync, 7 commits, clean |

Neither branch merged to `main`. Deliberately uncommitted and not mine: `package-lock.json`,
`.DS_Store`, two `guard_v2` md files, `command.txt`, `r.json`.

### Deploy commands

```bash
# gcloud configs: deployment-riplara's account was WRONG (testing.riplara). Fixed this
# session — it now carries deployment.riplara@gmail.com. Each project only accepts its own.
gcloud config configurations activate testing-riplara      # or deployment-riplara
gcloud run deploy tallybridge-parsing --source parsing --region asia-south1 --project tally-bridge-testing-env
gcloud run deploy tallybridge-backend --source backend --region asia-south1 --project tally-bridge-testing-env

# app (per command.txt)
flutter build web --dart-define-from-file=env/testing.json
firebase deploy --only hosting --project testing --account testing.riplara@gmail.com
```

---

## 6. Other bugs found, NOT fixed

**Tally silently drops zero-amount lines.** Verified: a 17-item payload with 3 at ₹0 produced
a 14-line Tally voucher; setting two to ₹100 produced 16. No warning. Matters because the new
rule yields ₹0 for never-sold items — **53% of the catalog has never been sold**.

**Voucher date is frozen at scan time, never refreshed on push.** A row sitting in `pending`
ages and lands in Tally carrying its scan date. 17 sale vouchers on testing were pushed on a
later day; worst lag **44 days**. This is the "date stuck on 3 Jul" incident. Client is
currently clean (0 lagged). Also: Cloud Run runs **UTC**, so a scan between 18:30–24:00 UTC
gets the previous IST day.

**`n8n/challan-to-invoice.json:187` has a hardcoded `date: "2025-07-30"`** in the legacy
Telegram→Sheet path.

**`scan_jobs.reason` records only *that* nothing matched**, never what was read or what it
nearly matched. Today's diagnosis needed the GCS image; it should have needed one query.
Add `ocr_text` + top-3 candidates with scores.

**`n8n/gsheet_appscript.js:2` contains a committed `service_role` JWT** for project
`hbaadljcliqzwjtpobex`.

**`push_queue` RLS is `USING(true) WITH CHECK(true)`** for all roles (`full_schema.sql:210`).

**Still open from the 2026-07-14 campaign:** INT-06 (party-match `strong_ratio>=0.99` bypass),
INT-09 (no duplicate check on the sale branch). Both release-blocking.

---

## 7. Gotchas for whoever picks this up

**Nav/tab indices are read in two places and coupled by position.** Hiding a tab bit twice:
the side-nav `RangeError` (`app_side_nav.dart` hardcodes `_item(0)…_item(4)` while
`app_bottom_nav.dart` iterates), and the scan badge counting the wrong queue
(`app_shell._activeQueueType` still derived purchase from a pinned index). If you hide
anything else, grep for every consumer.

**`created_at` on `push_queue` is not a creation time** — the app bumps it on edit
(`queue_screen.dart:188`). Use the `SALE-<YYYYMMDDHHMMSS>` voucher number instead.

**`Map.from` is shallow.** `cleanPayload` shares item maps with `_editableItems`, so mutating
one mutates the other. Bit once already.

**The app writes `push_queue` directly**, bypassing the Express backend. Anything that must
apply to app edits cannot live in `normalizePushVoucherPayload`.

**Test env files are byte-identical.** `.env.testing`, `backend/.env` and `parsing/.env` all
point at `yynuu`. "Am I on testing?" is unanswerable from file contents — check the URL.
`ztugw` = client.

**The VLM is non-deterministic.** The same MOHIT challan matched correctly in the morning and
became `ANAND SALES` later. A single replay shows what it reads *now*, not what it read then.

**There is no migration runner.** SQL is pasted into the Supabase SQL Editor, per project.

**Verify deploys by downloading the build archive** and diffing against HEAD —
`gcloud builds list --filter` by `services/<name>/` in the source object path, not `--limit 1`
(that returns the most recent build in the *project*, which may be a different service).

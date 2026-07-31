# TallyBridge — Sale Path: Global-Latest Rate + Party-Scoped Discount: Session Handout

Context-transfer doc for a new chat. Picks up after `session_handout_sale_discount.md`
(per-item discount) and the `ed5a3ad` merge (party-state lookup + fallback rate by
invoice date). This session **decoupled rate from discount** in the sale OCR pricing path.

Last updated: 2026-07-31.

---

## 0. TL;DR — the rule change

**Before** — a two-tier waterfall where rate and discount always came from the *same*
voucher row:

| Tier | Source | Filters | Result |
|---|---|---|---|
| 1 | RPC `get_latest_rates_for_party` | same party (`ILIKE`), **any voucher type**, `rate>0`, `is_cancelled=false` | `rate_source=same_party` |
| 2 | PostgREST on `voucher_items` | other parties, `voucher_type='GST SALE'` | `rate_source=different_party` |
| — | neither matched | — | `rate=0, discount=0, rate_source=none` |

**After** — two independent lookups:

- **Rate** = the latest GST SALE of that item to **anyone**. Party is irrelevant.
- **Discount** = the latest GST SALE of that item to **this party**. No history → **0%**.

### Why

Two problems with the waterfall:

1. **Party history acted as a stale anchor.** Once you had sold an item to a party, Tier 1
   pinned that party to *their* last rate forever, even when a newer and more representative
   sale existed elsewhere. Tier 2's good "latest sale wins" logic only ever ran for parties
   with no history.
2. **Tier 1 had no `voucher_type` filter.** A party who is both customer and supplier could
   have a sale priced off their last *purchase* — a supplier cost price on a customer
   invoice. That exact bug was fixed for Tier 2 in `5bcbc80` ("scope the sale rate fallback
   to GST SALE vouchers") but Tier 1 was never given the same guard.

The consequence of the new rule: **rate and discount now routinely come from different
vouchers.** That is intentional. Rate reflects the current market price of the item;
discount reflects the commercial terms with this specific customer.

---

## 1. Decisions

Every one of these was explicitly confirmed, not inferred. Where a decision knowingly
accepts a downside, that is recorded too.

| # | Question | Decision |
|---|---|---|
| 1 | Rate scope | `voucher_type = 'GST SALE'` only, **any party** |
| 2 | Discount scope | `voucher_type = 'GST SALE'` only, **this party** |
| 3 | Party never bought this item | discount = **0%** |
| 4 | Company scoping | **Unscoped** — no `company_id` filter (matches previous behaviour) |
| 5 | Rate tie on same invoice date | **Highest rate wins** |
| 6 | Discount tie on same invoice date | Discount on the **highest-rate row** (same rule as #5) |
| 7 | Hygiene filters | **None** — no `is_cancelled`, no `rate>0`, no unit match, no qty check |
| 8 | Party's last sale carried 0% | **0% is the answer** (literal latest, not last-non-zero) |
| 9 | Item with purchase history only | **rate = 0** — never borrow a cost price |
| 10 | Rate staleness | **No age limit** — a 3-year-old sale still supplies the rate |
| 11 | Provenance | Keep `rate_source` = `same_party｜different_party｜none`, recomputed. **Add** `discount_source` = `same_party｜none` |
| 12 | Query shape | **Two new batch RPCs** — 2 HTTP calls per invoice instead of 1+N serial calls |
| 13 | Index on `voucher_items(stock_item_name)` | **No** |
| 14 | Feature flag | **None.** Old helpers stay in the file, uncalled |
| 15 | Scope | **Backend only.** The Flutter app's own lookup is out of scope |
| 16 | Environment | **Testing Supabase (`yynuu…`) only.** Prod (`ztugw…`) untouched |
| 17 | Rate RPC fails / times out | Raise the client timeout to 3 min. If it still fails, **enqueue the zeros** |
| 18 | Party-name matching | **Exact, case-insensitive**: `lower(v.party_name) = lower(p_party_name)` |
| 19 | Item-name matching | **Exact** (`stock_item_name = ANY(...)`) |
| 20 | Record which sale supplied the rate | **No** `rate_source_party` / `rate_source_date` |
| 21 | Discount RPC fails but rate RPC succeeds | **Leave it** — `discount=0, discount_source=none` |
| 22 | How the old orchestrator is superseded | New `build_sale_rate_map_global`; `build_sale_rate_map` stays byte-identical and uncalled |
| 23 | Timeout shape | **Raise the shared constant** — `MINICPM_PUSH_QUEUE_TIMEOUT_SECONDS` 30 → 180 |
| 24 | `statement_timeout` on the new RPCs | **Skip** — the parsing service uses the service key, which has no limit |

---

## 2. Edge cases

This is the part worth reading before changing anything here again.

| # | Case | Behaviour |
|---|---|---|
| E1 | Item sold to this party before, but a *newer* sale exists to someone else | Rate from the other party's newer sale; discount from this party's older sale. **Rate and discount come from different vouchers** — this is the point of the change |
| E2 | Item never sold to this party, sold to others | Rate = global latest; discount = **0%** |
| E3 | Item never sold to anyone (GST SALE) | `rate=0, discount=0, rate_source=none, discount_source=none`. Line still enqueues at zero |
| E4 | Item only ever *purchased* (from this party or anyone) | `rate=0`. **Regression vs the old Tier 1**, accepted — never price a sale off a cost price |
| E5 | Two GST SALEs of the item on the same date, different rates | Highest rate wins |
| E6 | Same-date tie, same party, different discounts | Discount comes off the highest-rate row |
| E7 | Same-date **and** same-rate tie | Postgres picks arbitrarily. Not resolved further — `id` is a random UUID, so tie-breaking on it would add determinism without meaning |
| E8 | This party's latest sale carried 0% | 0% is the answer. A newer 0% line beats an older 45% line |
| E9 | Winning rate row is a **cancelled** voucher | Still used — no hygiene filters (#7). The old Tier 2 behaved the same way |
| E10 | Winning rate row has `rate = 0` or NULL | Used → line prices at 0 with `rate_source=different_party`. Under `rate DESC NULLS LAST` it only wins if it is the sole row on the latest date |
| E11 | Rate is 3 years stale | Used. No age limit; the app reviewer sees every line before activating. **Not detectable from the queued row**, since the winning voucher's date is not recorded (#20) |
| E12 | Party is both customer and supplier | Only their GST SALE rows count for the discount; their purchases are invisible to both queries |
| E13 | Multi-company database | Both queries span all companies (`company_id` unfiltered) — matches previous behaviour, but the risk widens now that rate ignores party |
| E14 | Rate RPC 500s or times out | Client timeout is 3 min (#17, #23). If it still fails: every line prices at 0, `rate_source=none`, the voucher **still enqueues**, reviewer fills rates in the app |
| E14b | Discount RPC fails, rate RPC succeeds | `discount=0, discount_source=none` — indistinguishable from genuine no-history (#21) |
| E15 | Party name contains `%` or `_` | Treated literally — `lower() = lower()`, not `ILIKE` (#18). The *old* `get_latest_rates_for_party` keeps its wildcard behaviour, and the Google Sheet still uses it |
| E16 | Item name differs in case/whitespace between stock master and `voucher_items` | No match → rate 0. Exact matching by #19; a mismatch is a data problem worth seeing, not papering over |
| E17 | `stock_matched` is `"NO MATCH"` or blank | Row dropped before pricing. If all rows drop, the scan is reported as a Garbage invoice |
| E18 | Existing `push_queue` rows in `pending` | Unaffected — already priced at enqueue time. Only new scans use the new rule |
| E19 | The Google Sheet tool (`n8n/gsheet_appscript.js`) | Unaffected — still calls the untouched `get_latest_rates_for_party` |
| E20 | The Flutter app's own rate lookup | Unaffected — still queries `voucher_items` with its own logic (out of scope, #15). A reviewer editing a line in the app may therefore see a rate that disagrees with the queued one |
| E21 | Prod (`ztugw…`) | New RPCs not applied there. Note `get_latest_rates_for_party` does not exist on prod either, so prod's Tier 1 already returns `{}` and everything falls to Tier 2 |
| E22 | Backend hangs during the enqueue POST | Side-effect of #23: `post_to_push_queue` now waits up to 3 min instead of 30s. The existing `ReadTimeout` handling (which deliberately does *not* mark the scan failed, because the row may already be committed) stays correct — the ambiguous window just gets longer |

---

## 3. Files changed

| File | Change |
|---|---|
| `supabase/migrations/20260731_sale_latest_global_rate.sql` | **new** — `get_latest_sale_rates_for_items(text[])` and `get_latest_party_discounts_for_items(text, text[])` |
| `supabase/migrations/20260731_sale_latest_global_rate_rollback.sql` | **new** — drops both |
| `parsing/server/handler.py` | timeout default 30 → 180; new `fetch_latest_sale_rates`, `fetch_latest_party_discounts`, `build_sale_rate_map_global`; `build_sale_voucher_payload` carries `discount_source` and strips both provenance keys from the pushed voucher |
| `parsing/server/test_sale_pricing.py` | **new** — offline stub-driven coverage of the edge cases above |
| `parsing/server/test_sale_gst_state.py` | stub repointed from `build_sale_rate_map` to `build_sale_rate_map_global` |
| `parsing/.env` | `MINICPM_PUSH_QUEUE_TIMEOUT_SECONDS` 8 → 180 (gitignored, local only) |

### ⚠️ The timeout lives in the env, not the code

**Locally**, raising the code default from 30 to 180 was a no-op on its own:
`parsing/.env:10` had `MINICPM_PUSH_QUEUE_TIMEOUT_SECONDS=8` and `env_value()` prefers the
env file over the default, so the effective timeout was 8 seconds. Raised to 180.

**On Cloud Run it resolves itself.** Verified 2026-07-31 on `tallybridge-parsing`
(testing): the service does **not** set `MINICPM_PUSH_QUEUE_TIMEOUT_SECONDS` at all, and
`parsing/.gcloudignore` + `parsing/.dockerignore` both exclude `.env` from the upload and
the image. So the container falls through to the code default — now 180. The service's own
request timeout is 3600s, so 180 fits comfortably.

`test_sale_pricing.py` prints a `WARN` line whenever the effective value is below 180
rather than asserting on it — the value is per-machine, so a hard assertion would fail on
any checkout that hasn't updated its `.env`.

**Deliberately NOT touched:**

- `get_latest_rates_for_party` — shared with `n8n/gsheet_appscript.js:42`.
- `fetch_latest_rates_for_party` / `fetch_fallback_rate_for_item` / `build_sale_rate_map` in
  `handler.py` — left byte-identical and uncalled, so reverting is a one-line change at the
  call site.
- The Flutter app.
- Prod Supabase (`ztugw…`).

---

## 4. The two RPCs

```sql
-- Rate: latest GST SALE of each item to ANYONE. party_name is returned only so the
-- caller can label rate_source same_party vs different_party; it is not persisted.
CREATE OR REPLACE FUNCTION public.get_latest_sale_rates_for_items(p_item_names text[])
 RETURNS TABLE(stock_item_name text, rate numeric, party_name text)
 LANGUAGE sql STABLE
AS $function$
  SELECT DISTINCT ON (vi.stock_item_name)
    vi.stock_item_name, vi.rate, v.party_name
  FROM voucher_items vi
  JOIN vouchers v ON v.id = vi.voucher_id
  WHERE vi.stock_item_name = ANY(p_item_names)
    AND v.voucher_type = 'GST SALE'
  ORDER BY vi.stock_item_name, v.date DESC NULLS LAST, vi.rate DESC NULLS LAST;
$function$;

-- Discount: latest GST SALE of each item to THIS party. Same tie-break as the rate
-- query, so on a same-date tie both answers come off the highest-rate row.
CREATE OR REPLACE FUNCTION public.get_latest_party_discounts_for_items(
  p_party_name text, p_item_names text[])
 RETURNS TABLE(stock_item_name text, discount_pct numeric)
 LANGUAGE sql STABLE
AS $function$
  SELECT DISTINCT ON (vi.stock_item_name)
    vi.stock_item_name, vi.discount_pct
  FROM voucher_items vi
  JOIN vouchers v ON v.id = vi.voucher_id
  WHERE vi.stock_item_name = ANY(p_item_names)
    AND v.voucher_type = 'GST SALE'
    AND lower(v.party_name) = lower(p_party_name)
  ORDER BY vi.stock_item_name, v.date DESC NULLS LAST, vi.rate DESC NULLS LAST;
$function$;
```

**Why `NULLS LAST` on both keys.** Plain `DESC` in Postgres is NULLS FIRST, so a NULL
`vouchers.date` would outrank every dated voucher, and a NULL `rate` would win every
same-date tie. The old Tier 1 RPC has exactly that latent bug (`ORDER BY … v.date DESC`);
Tier 2 already guarded it with `nullslast`. This is **not** a hygiene filter — no rows are
excluded, they are only ordered sanely.

**There is no migration runner in this repo.** Apply by pasting into the Supabase SQL
Editor, per project. Applied to `yynuu…` (testing) only.

---

## 5. Payload shape

`voucher_payload.items[]` is unchanged — 7 keys, no provenance:

```json
{ "stock_item_name": "...", "quantity": 5.0, "rate": 1905.0, "amount": 9525.0,
  "discount_pct": 0.0, "unit": "NOS", "godown_name": "Main Location" }
```

`source_payload.items[]` gains one key (`discount_source`); everything else is as before:

```json
{ "stock_item_name": "...", "quantity": 5.0, "rate": 1905.0, "amount": 9525.0,
  "discount_pct": 0.0, "unit": "NOS", "godown_name": "Main Location",
  "source": "Matching_Algorithem", "score": "56%",
  "rate_source": "different_party", "discount_source": "none" }
```

`source_payload` is inserted verbatim by the backend (no whitelist), so the new key needs
no backend change. `voucher_payload` **is** whitelisted by `normalizePushVoucherPayload`,
which is why both provenance keys must be stripped before the push.

Pricing math is unchanged:
`gross = qty × rate`, `amount = gross × (1 − discount_pct/100)`, GST on the **net** subtotal.

---

## 6. Verification

1. **Confirm the target DB first.** `.env.testing`, `backend/.env` and `parsing/.env` are
   currently byte-identical and all point at `yynuu…` (testing). Prod is `ztugw…`, reachable
   only via `.env.deployment` / Cloud Run. Check `SUPABASE_URL` in `parsing/.env` before
   running anything.
2. Paste `20260731_sale_latest_global_rate.sql` into the Supabase SQL Editor for `yynuu…`.
3. Sanity-check both RPCs with `curl` against a known item — e.g. the
   `HSS TAP 1" BSW SET TOTEM` / `HARYANA` case from `53014c9`, whose expected answer
   (12606 @ 45%, ISH TRADING 2026-06-27) is already documented — and confirm the rate now
   resolves globally while the discount resolves per party.
4. `cd parsing && python server/test_sale_pricing.py` → exit 0 (offline, no network).
   Watch for the `WARN` line about the timeout — if it prints, the env still has the old
   value and the rate lookup will fail under load.
5. `python server/test_sale_gst_state.py` → exit 0.
6. End-to-end: start `backend` (`npm run dev`) and the parsing server, POST a sale image to
   `/?type=sale&push=queue&company=K V ENTERPRISES`, then read the new `push_queue` row and
   assert: `voucher_payload.items[]` carries **no** `rate_source`/`discount_source`;
   `source_payload.items[]` carries both; `amount == round(qty*rate,2) * (1 - disc/100)`;
   `Σ amount == ledger_entries["GST SALE"].amount`. That last one is load-bearing —
   `tally_pusher.py` rejects the push if inventory ledger total and item total differ by
   more than 0.05.
7. Spot-check one line by hand against the two RPCs.

### Verified on `yynuu…` (2026-07-31)

Both RPCs applied and answering. Live E1 case, `HSS DRILL 4 MIRANDA`:

| | party | date | rate | disc |
|---|---|---|---|---|
| global latest sale | BALAJI H/W AGENCIES | 2026-07-24 | 108.10 | 76% |
| our customer's own last buy | SHREE SHYAM HARDWARE STORE | 2025-07-30 | 113.00 | 0% |

Billing SHREE SHYAM under the new rule gives **rate 108.10** (from BALAJI, the newer sale)
and **discount 0%** (SHREE SHYAM's own history) — `rate_source=different_party`,
`discount_source=same_party`. Rate and discount from two different vouchers, as intended.
On qty 10: new 1081.00, old 1130.00.

### ⚠️ The no-history case swings hard, and in the other direction

The same data shows the change that costs the most money. `53014c9` made the old tier-2
fallback **carry the borrowed sale's discount**. So a customer with no history for
`HSS DRILL 4 MIRANDA` used to be billed BALAJI's 108.10 **@ 76%** → 259.44 on qty 10.
Under decision #3 they now get 108.10 **@ 0%** → 1081.00. That is a **4.2× swing** on
exactly the customers you know least about.

This is intended — a discount is a term you negotiated with one customer and should not
leak to another — but it is the single biggest practical effect of this change, and it
reverses a deliberate decision from a week earlier. Worth eyeballing the first few
new-customer invoices after this ships.

---

## 7. Still open (not addressed here)

- **The end-to-end scan (step 6 of §6) has not been run** — the RPCs and the merge are
  verified, but no invoice has been pushed through the full path yet.
- **Deploy is `tallybridge-parsing` only.** Nothing in `backend/` changed —
  `source_payload` is inserted verbatim, so `discount_source` needs no backend work.
  Note the active gcloud config is `deployment-riplara`; the command in `command.txt:15`
  carries an explicit `--project tally-bridge-testing-env`, but activating
  `testing-riplara` first is safer.
- **INT-06** party-match `strong_ratio >= 0.99` bypass (wrong customer) — release-blocking.
- **INT-09** no `check_duplicacy` on the sale branch (duplicate sales) — release-blocking.
- Prod (`ztugw…`) has neither the old nor the new rate RPC. Decide what prod should run
  before this ships to a client.
- The Flutter app's independent rate lookup (`voucher_detail_sheet.dart`) still uses the
  old logic, so an in-app edit can disagree with the queued rate (E20).
- `n8n/gsheet_appscript.js:2` contains a **committed Supabase service_role JWT** for project
  `hbaadljcliqzwjtpobex`. Unrelated to this change, but it is in the repo.

See `md_files/session_handout_sale_flow_testing_2026-07-14.md:150-165` for the full
defect list.

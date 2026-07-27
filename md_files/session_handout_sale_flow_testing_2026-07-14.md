# Session Handout — Sale Flow Consumer-Breakage Testing (2026-07-14)

> Handoff for a fresh chat continuing the **Sale Flow** test campaign. Read this
> top-to-bottom before doing anything. It has the environment, the four test
> methods, the code findings, results so far, what's left, and the pending
> cleanup you inherit.

> ⚠️ **Secrets policy:** this file (md_files/) is **NOT gitignored**. It deliberately
> contains **no raw keys**. Source all credentials from `/.env.testing` (repo root)
> and the GCP Cloud Run `tallybridge-parsing` env. Do not paste keys here.

---

## 1. What this is

Executing the 80-case tracker **`/Users/siddharth/Documents/sale_flow_consumer_breakage_tracker_2026-07-13.xlsx`**
(sheet "Sale Test Tracker") against the deployed TallyBridge **sale** pipeline.
Goal: find consumer-breakage / release-blocking defects in scan → parse → match →
queue → push-to-Tally.

### Tracker layout (after this session's edit)
Header row 4. Columns: **A** Test Case ID · **B** Consumer Attempt · **C** Steps ·
**D** Expected · **E** Done (☐/☑ dropdown) · **F** pass/fail (Pass/Fail dropdown,
green/red conditional formatting — added this session) · **G** Actual Results/Remarks.

Section header rows: **A**=5 (SALE-INT-01..12), **B**=19 (SALE-Q-01..12),
**C**=33 (SALE-SHEET-01..08), **D**=43 (SALE-EDIT-01..19), **E**=67 (Create New Item),
**F**=79 (Push to Tally). Backup: `...BACKUP.xlsx` in the same folder.

### ‼️ Behavioral rules (the user was explicit)
1. **Do NOT write pass/fail or remarks into the Excel** — the user enters them
   themselves. You just hand them a **concise + complete** remark string.
2. **Do NOT delete any test artifact (push_queue / scan_jobs rows) without asking** —
   the user screenshots them. Delete only when told, and **only by exact `id`**
   (broad/pattern deletes on the shared testing Supabase get blocked by the safety
   classifier — one row per `id=eq.<uuid>`).
3. Keep remarks tight: a couple sentences — verdict, mechanism, code ref, fix.

---

## 2. Environment & credentials

- **App under test:** `https://tallybridge-testing-env-636d4.web.app` (Sale tab).
  Reads the **testing** Supabase project.
- **Supabase (testing):** project `yynuuysvjeipawzfbeme`, URL
  `https://yynuuysvjeipawzfbeme.supabase.co`. **Service key + backend API key are in
  `/.env.testing`** (`SUPABASE_SERVICE_KEY`, `API_KEY`). The backend API key value is
  the `sb_publishable_…` string in that file.
- **Deployed (GCP Cloud Run, project `tally-bridge-testing-env`, asia-south1):**
  `tallybridge-parsing` (parser) and `tallybridge-backend`. App scans go
  phone → GCP parser → GCP backend → Supabase.
- **Company:** `K V ENTERPRISES` (the only row in `companies`).
- **Sale OCR = RunPod MiniCPM.** Env (from GCP `tallybridge-parsing`):
  `SALE_OCR_BACKEND=runpod`,
  `MINICPM_RUNPOD_URL=https://api.runpod.ai/v2/vllm-l3ra5g9jipnoeu/openai`,
  `MINICPM_RUNPOD_API_KEY=<account rpa_ key — get from GCP parsing env or RunPod dashboard>`
  (same value as `RUNPOD_POD_API_KEY`), `MINICPM_RUNPOD_TIMEOUT_SECONDS=2000`.
  Only needed for tests that actually run OCR.

### Key tables (Supabase REST: `/rest/v1/<table>`, headers `apikey` + `Authorization: Bearer <service key>`)
- **`scan_jobs`** — the "Processing…" badge + garbage signal. `type` (sale/purchase),
  `status` (processing/failed), `reason`, `page_count`, `created_at`. Mobile INSERTs on
  scan-send (badge climbs); parser DELETEs after the push_queue insert (badge drains),
  or PATCHes `status='failed'` for a garbage scan. **Clients ignore rows older than 300s**
  (client-side TTL). Schema: `backend/supabase_scan_jobs.sql`.
- **`push_queue`** — the queued vouchers. Columns: `id, company_id, voucher_payload (jsonb),
  source_payload, status (pending/…), error_message, created_at, pushed_at, edit_state`.
  Party/items live inside `voucher_payload`. (No top-level `party_name`/`voucher_type` cols.)
- **`vouchers`** — 45k rows; source of **party-match candidates** (`party_name` column).
- **`stock_items`** — `name, unit, rate, part_code, …` (stock-match candidates).
- **`companies`** — one row, `K V ENTERPRISES`.

---

## 3. The four test methods (pick per what the case exercises)

**Physical/phone scan is NOT usually required.** Choose:

| Method | Use for | How |
|---|---|---|
| **A. Scan-and-observe** | OCR/parse/match behavior (needs real invoice content) | User scans a challan in the app (or you curl an image to a local parser). You poll `scan_jobs` + `push_queue`. |
| **B. Direct API POST** | Backend validation, XML, storage (INT-11, INT-12) | `POST http://localhost:3001/api/sync/push-queue` (or GCP backend) with header `x-api-key: <API_KEY>`. No scan. |
| **C. Seed-and-drive** | App UI: queue screen, viewing, editing (sections B/C/D) | You inject a `push_queue` row (via method B, or a direct Supabase insert), then the **user drives the app**; you verify the DB. No scan. |
| **D. Local injection** | Timing/timeout races (INT-04 parser delay, INT-10 backend delay) | Rebuild the local stack with an **env-gated code hook**; revert after. |

### "What to send" for a valid scan (method A)
Party **`BALAJI H/W AGENCY`** (fuzzy-matches real debtor `BALAJI H/W AGENCIES`).
Real items that match masters, e.g. `AIR IMPACT WRENCH`, `MAGNET`, `C-10`,
`HSS TAP`. Challan images used this session are in the chat's image-cache.

### Running the local stack (methods B/C/D)
1. `cp .env.testing backend/.env` (backend reads it).
2. Create `parsing/.env` with Supabase URL + service key, `MINICPM_PUSH_QUEUE_API_KEY`
   (= backend API_KEY), and the RunPod block above. (Both `.env` files ARE gitignored.)
3. Backend: `cd backend && npm run dev` → port 3001.
4. Parser (only if OCR): `cd parsing/server`, export the env vars, `python3 handler.py --serve` → port 5003.
   ⚠️ **`tsx watch` gotcha:** killing only the child leaves the watcher, which respawns a
   new child WITHOUT your env. Kill the whole tree: `pkill -f "tsx watch src/index.ts"`,
   then verify the listening pid's env with `ps eww -p <pid>`.
5. Curl a sale scan to the parser:
   `POST http://localhost:5003/?type=sale&push=queue&company=K+V+ENTERPRISES&job_id=<uuid>`
   with the image as the raw body; `job_id` must be a `scan_jobs` row id you inserted first.

---

## 4. Code mechanisms & line refs (the findings map)

**Parser sale flow — `parsing/server/handler.py`:**
- Sale branch: `~2246` (`type=sale`, `push=queue`). Reaches `post_to_push_queue` then `delete_scan_job`.
- **Party match** `_match_party_from_image` accept rule (`:504`): `score >= 78 OR strong_ratio >= 0.99`.
  Generic tokens (AGENCY, HARDWARE, TRADERS, ENTERPRISES…) are stripped, so a 1-distinctive-token
  name → `strong_ratio=1.0` → **auto-accept bypassing the 78 floor** → **INT-06 defect**.
- **Stock match**: an item is dropped only if `stock_matched` is empty/`"NO MATCH"` (`:1286`).
  The picked candidate is `matches[0]` with **no min-similarity floor** (`test_minicpm.py:1606`);
  narrowing (token/number/brand overlap) is the only gate → **INT-07 defect**.
- **Qty fallback** (`:1297`): `parse_sale_quantity` returns 0.0 for non-numeric/negative/blank,
  then `if quantity <= 0: quantity = 1.0` — **silent**, no flag → **INT-08 defect**.
- **Sale voucher number** (`:1353`): `SALE-{YYYYMMDDHHMMSS}` (timestamp). **No `check_duplicacy`
  in the sale branch** (purchase-only) → **INT-09 defect**.
- **ReadTimeout handling** (`:2306`): on a `requests.exceptions.ReadTimeout` from the push, it
  does **NOT** `report_failed_scan` (avoids bogus garbage when backend already committed) → **INT-10 pass**.
  Client push timeout `MINICPM_PUSH_QUEUE_TIMEOUT_SECONDS` **default 30s**.

**Backend — `backend/src/routes/sync.ts`:**
- Push-queue route `:2315`. Validation `normalizePushVoucherPayload` `:164` → **400 before insert**
  (voucher_type allowed-list, date, party required, non-empty ledger_entries, per-item fields).
- **Date validator `normalizeIsoLikeDate` `:128` is FORMAT-ONLY** (`^\d{4}-\d{2}-\d{2}$`) — accepts
  impossible values like `2026-13-45` → **INT-11 gap**.
- Order: **insert row (`:2382`) → slow GCS image upload (`:2403`) → respond (`:2409`)**. Images are
  best-effort (try/catch); stored in GCS **keyed by row id**, independent of the HTTP response.
  Image serving: `GET /push-queue/:id/image/:page` `:2432`. **Local backend has NO `INVOICE_BUCKET`
  → images not stored locally** (that's why local-stack vouchers show no image).

**Tally XML push — `src/python/tally_pusher.py`:**
- `_build_voucher_xml_block` `:356`; `_xml_escape` = `xml.sax.saxutils.escape` (escapes `& < >`).
  All name fields (party, item, ledger, voucher#, narration, godown) are escaped → **INT-12 pass**.
- Emits Tally's magic `&#4;` (U+0004) in `<INDENTNO>`/`<ORDERNO>` = "Not Applicable" marker —
  not valid XML 1.0 (a generic parser rejects it) but **Tally's own convention, input-independent**.

---

## 5. Results so far — Section A (SALE-INT)

INT-01/02/03 were already ☑ before this session. This session:

- **INT-04 — PASS** (verified live, local parser delay). Parser >5min: badge drains at the 300s
  client TTL while the row is still `processing`; when it enqueues, exactly one row, no dup/ghost.
  Note: `delete_scan_job` skipped on that path is not relevant here (normal delete ran).
- **INT-05 — PASS.** Blank / unreadable / misdirected docs → `failed` garbage rows with reason +
  `page_count` (image viewable), no voucher, not stuck. **Note:** a *readable* junk doc took ~7.5 min
  to resolve (OCR + full-image retry) — past the 300s TTL, so the badge cleared before the garbage
  row appeared (temporary "invisible scan"). Landed eventually; UX latency worth flagging.
- **INT-06 — FAIL (release-blocking, wrong-customer).** `AARAV TRADERS` (unknown) → matched real
  debtor `AARAV ENTERPRISES` and queued. Cause: `strong_ratio>=0.99` single-shared-token bypass (`:504`).
- **INT-07 — FAIL (no stock floor).** Gibberish items (Zorbex Flimangle…) force-matched real stock
  (MAGNET 60%, IRWIN PANTOGRAPH 45%, FLAP DISK 45%) → voucher built, pushable. Qty read correctly so
  not hallucination. Mitigation: "Needs Attention!!" flag + ₹0, but **push not blocked**.
- **INT-08 — FAIL.** Invalid qty 0→1, −5→5 (sign dropped), abc→1, **silently**, labeled green
  "Confirmed match · No need to edit", pushable. `parse_sale_quantity` + `:1297`.
- **INT-09 — FAIL (release-blocking, duplicate sales).** Same challan twice → 2 pending vouchers,
  both pushable, no warning; timestamps collided so even `voucher_number` was identical. No dedup in sale branch.
- **INT-10 — PASS (verified live, local backend delay).** Backend commits row then slow response →
  client `ReadTimeout`: exactly 1 committed voucher, `scan_jobs` stayed `processing` (NOT garbage),
  findable after refresh. Cosmetic: badge lingers to 300s TTL.
- **INT-11 — PASS w/ date gap.** Malformed payloads → clear 400, no row (blank party, bad voucher_type,
  empty ledgers, missing item fields, non-numeric amount, bad-FORMAT dates). **GAP:** date is format-only
  (`:128`) — `2026-13-45` accepted (200), stored raw, UI silently coerces display to 14/02/2027.
- **INT-12 — PASS.** Special chars/Unicode/long/decimals preserved (storage round-trip API→DB, and
  `_build_voucher_xml_block` escaping — `& < >` escaped, all fields recovered exactly). Note the `&#4;`
  Tally marker. Live Tally import not run (no Tally instance).

**Every remark above is already written in "concise + complete" form for the user to paste.**

---

## 6. What's left

- **Section B — SALE-Q-01..12** (Sale Queue screen): Q-01/02/03 already ☑. Rest are **seed-and-drive**
  (ordering, pull-to-refresh, edit markers, stale writes, two-user conflicts, garbage open/delete,
  stock-item job rendering, refresh-failure reconciliation). Q-07/Q-08 need a row **with images** (garbage
  image) → needs GCP path or a configured `INVOICE_BUCKET` locally.
- **Section C — SALE-SHEET-01..08** (viewing/navigation): SHEET-01/03/04 already ☑. Rest seed-and-drive;
  SHEET-06 (image fetch failure) needs stored images.
- **Section D — SALE-EDIT-01..19** (header/line editing): EDIT-01/02/03 already ☑. Seed a pending row,
  user edits in app, verify persisted `voucher_payload` / `edit_state`.
- **Section E — Create New Item**, **Section F — Push to Tally** (F needs a real Tally instance on :9000 —
  not available this session).

Recommended next: start **Section B** via seed-and-drive (inject the exact row per case, user clicks through).

---

## 7. ‼️ Pending cleanup you inherit

The local stack is **already stopped** (ports 3001/5003 free after the session boundary). Outstanding:

1. **Code hook to revert** (INT-10): `backend/src/routes/sync.ts` still has the `TB_TEST_PUSH_DELAY_MS`
   block (env-gated no-op, but a real diff). Revert before any commit: `git checkout backend/src/routes/sync.ts`.
   (The `parsing/server/handler.py` `MINICPM_TEST_PARSER_DELAY` hook from INT-04 was **already reverted**.)
2. **Env files to delete at final cleanup:** `backend/.env`, `parsing/.env` (both gitignored).
   *Keep them if you're continuing local seed-and-drive tests* (backend needs `backend/.env`).
3. **Test rows left in Supabase `push_queue`** (kept per the user's "don't delete" for screenshots) —
   confirm with the user before removing, delete by exact `id`:
   - INT-10: 2 × BALAJI ₹2,76,982 (created ~15:06 and 15:10 UTC).
   - INT-11: 2 × BALAJI ₹100 (created 15:26 UTC) — one has the impossible date `2026-13-45`.
   - INT-12: 1 × special-char BALAJI voucher.
   - Also lingering `scan_jobs`: INT-05's 3 `failed` garbage rows + any `processing` rows past TTL.
4. **GCP untouched** the whole time; never modify it.

---

## 8. Quick-start for the new chat
1. Read this doc. 2. Confirm with the user which section to run and whether to clean up first.
3. For B/C/D: bring up the local backend (`cp .env.testing backend/.env && cd backend && npm run dev`),
   seed a row per case via `POST /api/sync/push-queue`, have the user drive the app, verify the DB.
4. Give the user a concise+complete remark per case; **they** fill the Excel.
5. Delete artifacts only when told, by exact `id`.

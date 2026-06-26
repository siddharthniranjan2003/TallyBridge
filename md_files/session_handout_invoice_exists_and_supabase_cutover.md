# Session Handout — `invoice_exists` Feature, GCP Cutover, push_now Gate, Duplicacy Check

**Date:** 2026-06-10
**Branch:** `TallyBridge-Backend-Refactor`
**Scope:** Everything done this session — added an `invoice_exists` flag to the purchase push pipeline, deployed it to GCP Cloud Run, untangled a `yynuu`/`ztugw` Supabase mix-up, diagnosed a "pending vouchers auto-push to Tally" incident, and documented the duplicacy check.

---

## 0. TL;DR — current live state (as of handoff)

- **Both Cloud Run services are LIVE on the `yynuu` Supabase** (the production DB), with my code:
  - `tallybridge-backend` → serving revision **`00021-854`**, image **`sha256:9c841fce…`** (my `invoice_exists` build), env `SUPABASE_URL=yynuu`, `API_KEY=sb_publishable_c2Co…`
  - `tallybridge-parsing` → serving revision **`00025-dxq`**, image **`sha256:4923e0c8…`** (a rebuild that DOES include my `handler.py` change — proven by a live test), env `SUPABASE_URL=yynuu`, `MINICPM_PUSH_QUEUE_API_KEY=sb_publishable_c2Co…`
- **Keys match** (`c2Co` = `c2Co`) → parsing→backend enqueue auth works (no 401). Both `/health` = 200.
- **`invoice_exists` is working end-to-end** — verified live: a fresh `emkay` purchase (`GN25Y-32738`) landed in `push_queue` with `"invoice_exists": false`.
- **`push_now` approval gate is now enforced** (backend `00021` is built from current source) → pending vouchers no longer auto-push to Tally.

---

## 1. The feature: `invoice_exists` on the `push_queue` row

**Goal (user request):** make the duplicacy result visible **inside `voucher_payload`** on the Supabase `push_queue` row (not just in the API response).

### Two code edits (both shipped & live)

**1. Parsing — inject before enqueue** — `parsing/server/handler.py`, in the `/docstrange?purchase=all&source=runpod` branch (the runpod purchase push path), right before `post_to_push_queue`:
```python
# Persist the duplicacy result on the enqueued voucher so it
# is visible on the push_queue row's voucher_payload.
tally_payload = queue_request_payload.get("tally_payload")
if isinstance(tally_payload, dict) and isinstance(
    tally_payload.get("voucher_payload"), dict
):
    tally_payload["voucher_payload"]["invoice_exists"] = invoice_exists
queue_response = post_to_push_queue(queue_request_payload, company_name)
```

**2. Backend — keep it through normalization** — `backend/src/routes/sync.ts`, in `normalizePushVoucherPayload`'s whitelisted return object (≈ line 287):
```ts
invoice_exists: normalizeBoolean(raw.invoice_exists) ?? false,
```
This was **required** because the backend rebuilds `voucher_payload` field-by-field (a whitelist), so any extra key is otherwise dropped.

### Behavior
- The field is **always present** on new rows (backend defaults it to `false`).
- `true` = invoice number already in the `vouchers` table (duplicate); `false` = not found / no number / sale path.
- **Purchase only** — the sale path never calls the duplicacy check, so sale rows just get the `false` default.
- **Only NEW rows** (pushed after the 2026-06-10 promotion) have the field; older rows do not.
- `tally_pusher.py` reads `voucher_payload` via explicit `.get()` per field, so the extra key is ignored by the Tally XML builder — push is unaffected.

### Verified live
- `emkay` `GN25Y-32738` → `invoice_exists: false` ✅ (not a duplicate)
- To see a `true`: push `addison.pdf` (`GRTW-044658-2526`) — that invoice exists in `vouchers`.

---

## 2. The duplicacy check (reference)

`check_duplicacy()` — `parsing/server/handler.py:1640`:
```python
GET {SUPABASE_URL}/rest/v1/vouchers?voucher_number=eq.{invoice_number}&select=id
# True if HTTP 200 AND len(json) > 0
```
- Input = parsed OCR header `parsed.header.invoice_number`.
- Auth = parsing `SUPABASE_KEY` (must be **service_role** or RLS hides rows → always false; see [[parsing-supabase-service-role-key]]).
- Called only on **purchase** paths: `/docstrange?purchase=all` (`:1955`), `/raw-vlm?invoice=purchase` (`:1874`), `/?type=purchase&check=duplicacy` (`:2034`). **No sale path.**

### Limits (important)
- **Flag, not a gate** — the voucher is enqueued/pushed **regardless** of `invoice_exists` (`post_to_push_queue` runs either way at `handler.py:1980`). There is **no duplicate prevention**, only reporting.
- **Exact string match** (`eq.`) — OCR variance (`/` vs `-`, space, case) → miss.
- **Checks `vouchers`, not `push_queue`** — an invoice just enqueued (pending, not yet synced to `vouchers`) is NOT flagged.
- **No company scoping** in the query — matches `voucher_number` across the whole `vouchers` table.
- **Fails open** — any error → `False`.
- Sales use a generated unique `voucher_number` (`SALE-YYYYMMDDHHMMSS`), so a duplicacy check would never match anyway.

---

## 3. The pipeline (sale/purchase PDF → Supabase `push_queue`)

Two hops. The OCR parsing server reads + builds the voucher; the backend is the only thing that writes `push_queue`.

```
PDF/image → parsing/server/handler.py (Cloud Run "tallybridge-parsing", port 5003)
          → OCR (RunPod) → vendor parse → stock match → voucher_payload
          → post_to_push_queue() → POST {backend}/api/sync/push-queue  (x-api-key)
          → backend sync.ts:2238 normalizePushVoucherPayload → INSERT push_queue {status:"pending"}
          → (later) src/main/push-queue-poller.ts polls → tally_pusher.py → TallyPrime
```

| Path | Doc types | Route | Push trigger |
|---|---|---|---|
| **Sale** | image/pdf (handwritten challan) | `POST /?type=sale&push=queue` | `handler.py:2080–2104` (`build_sale_voucher_payload`) |
| **Purchase** | image + pdf | `POST /docstrange?purchase=all&source=runpod` | `handler.py:1962–1988` |

- Purchase via `/?type=purchase` is **parse-only** (no enqueue).
- Sale OCR reads the **party name** (MiniCPM-V RunPod worker); purchase uses Nanonets-on-RunPod (`Nanonets-OCR2-3B`).

---

## 4. GCP deployment — services, revisions, the cutover saga

### Services (project `tallybridge-test-ocr`, region `asia-south1`)
| Service | URL | Source | Notes |
|---|---|---|---|
| `tallybridge-backend` | `tallybridge-backend-…asia-south1.run.app` | `--source backend`, `backend/Dockerfile`, 1Gi | the cloud API |
| `tallybridge-parsing` | `tallybridge-parsing-…asia-south1.run.app` | `--source parsing`, `parsing/Dockerfile`, 2Gi/2cpu | the OCR server |

### Documented deploy method (from `session_handout_gcp_migration_discount.md`)
```bash
gcloud run deploy tallybridge-backend  --source backend  --region asia-south1 --project tallybridge-test-ocr
gcloud run deploy tallybridge-parsing  --source parsing   --region asia-south1 --project tallybridge-test-ocr \
  --min-instances=1 --memory=2Gi --cpu=2 --timeout=3600
```
- `--source` deploy **preserves env vars & scaling** unless overridden.
- `--allow-unauthenticated`/`allUsers` IAM are **blocked for the agent** (user runs them) — but **not needed on redeploy** (IAM persists).
- Both `parsing/.gcloudignore` and `backend/.gcloudignore` exist (exclude the 4.4 GB `.venv-ocrvl`, secrets, samples) → upload stays small.

### Revision timeline this session (the messy part)
- I deployed `--source` → **backend `00020-cft`** (img `9c841fce`) and **parsing `00022-49z`** (img `ae49ed93`), both with my code.
- **GOTCHA #1 — pinned traffic:** because traffic was explicitly pinned to old revisions, `gcloud run deploy` (even without `--no-traffic`) created the new revisions at **0% traffic**. New code does NOT go live until you `update-traffic`.
- I promoted `00020`/`00022` → they pointed at **`ztugw`** (inherited template env) — the WRONG DB.
- **GOTCHA #2 — key mismatch (401):** after promoting, backend `API_KEY` was the `ztugw` key (`IRs…`) but parsing's `MINICPM_PUSH_QUEUE_API_KEY` was `c2C…` → every enqueue would 401. (Matches [[parsing-gcp-redeploy-gotchas]].)
- User edited env back to `yynuu` → created **backend `00021-854`** (img `9c841fce` + `yynuu` env) and **parsing `00023…00025-dxq`** (img `4923e0c8`, a source rebuild + `yynuu` env), again at **0% traffic** (pinned-traffic gotcha again).
- I promoted **`00021`** (backend) and **`00025`** (parsing) → **LIVE on `yynuu`, keys matched (`c2Co`)**. Confirmed working.

### GOTCHA #3 — template vs serving-revision env
`spec.template…env` (next-deploy config) can differ from the **serving revision's** actual env. Always check the *revision* (`gcloud run revisions describe <rev>`), not just the service template, to know what's actually running.

### GOTCHA #4 — env edit ≠ code deploy; env edit keeps the image
- Editing env in the console creates a new revision **with the same image** (so my code stayed intact across the user's `yynuu` env edit).
- Conversely, a console env-only deploy does **not** ship code changes — those need `--source`.

---

## 5. The `yynuu` vs `ztugw` Supabase identity (resolved)

This caused major confusion. Resolution:
- **`yynuu` (`yynuuysvjeipawzfbeme`) = the live PRODUCTION Supabase** — what the pipeline uses, where all the test data + the client's real data live. This is "my supabase" per the user.
- **`ztugw` (`ztugwhevemibdrzqafyw`) = a secondary DB**, referenced by the backend `*_CLIENT` env (which feeds ONLY `/reorder-levels`) and the local `.env_clinet` file. The `.env_clinet` key is an **anon** key (decoded: `role:anon`), NOT service_role.
- Cloud Run **template** keys for both are `service_role` (decoded & verified) for whichever DB they target.
- The `push_now`/auth key in use on `yynuu` is `sb_publishable_c2Co…`; the `ztugw` one is `sb_publishable_IRsM…`.

**Lesson:** don't trust the `*_CLIENT`/`.env_clinet` naming as "the client DB to cut over to." `yynuu` is production.

---

## 6. The "pending vouchers auto-push to Tally" incident

**Symptom:** client reported a `balaji` sale pushed into TallyPrime with nobody pressing "push." A screenshot of the `yynuu` `push_queue` showed test purchases (`ADDISON`, `DIE 18 X 2.5`/Totem) as `status=failed` with a real Tally `tally_response` ("Stock Item … does not exist") — proving they were **pulled and pushed to Tally without activation**.

**Design (current source):**
- Insert = `status:"pending"` (`sync.ts:2289`).
- `GET /push-queue` (what the desktop poller reads) filters `eq("status","push_now")` (`sync.ts:2410`).
- `/push-queue/activate` flips `pending → push_now` (`sync.ts:2363`).
- Desktop side: `push-queue-poller.ts` (5s loop) → `sync_main.py (poll_push_queue)` → `cloud_pusher.fetch_pending_push_vouchers()` → `GET /api/sync/push-queue` (no status param; server filters `push_now`).

**Root cause:** the **previously-serving** backend predated the gate. Commit history:
- `3af32a4` — original GET filtered `eq("status","pending")` → **auto-pushed everything**.
- `feeb928` (2026-05-20) — changed gate to `push_now` (approval step).

The live revision before this session was serving the un-gated behavior. **Deploying current source (backend `00021`) now enforces `push_now`** → fixes the incident. **Behavior change for the client:** parsed vouchers now stay `pending` until activated; they no longer auto-push.

---

## 7. Test data pushed this session (all in `yynuu` `push_queue`)

Sent to the live parsing endpoint during testing (purchase via `/docstrange?purchase=all&source=runpod`, sale via `/?type=sale&push=queue`):
- 2026-06-07: `cp` (CP, inv 1280/2025-26), `pidilite` (no inv #), `emkay` (ET, GN25Y-32738), `saint global` (GNL, DL1000033666), `balaji` sale (BALAJI H/W AGENCIES, 20 items), `addison` (ADDISON, GRTW-044658-2526, `invoice_exists:true`), `totem` (DIE 18 X 2.5).
- These are **pre-feature** rows (no `invoice_exists`) and several are `status=failed` (stock item not in Tally). **Cleanup candidate** — they're test junk in the production queue.

---

## 8. Open items / next steps

- [ ] **Clean up test rows** in `yynuu` `push_queue` (the 06-07 `cp/pidilite/emkay/saint global/balaji/addison/totem` rows, esp. the `failed` and duplicate ones).
- [ ] **Commit the code changes** — `handler.py` + `sync.ts` `invoice_exists` edits are deployed from the working tree but **not git-committed**. Also note pre-existing uncommitted `parsing/purchase/voucher_builder.py`, `parsing/test_minicpm.py`.
- [ ] **Align the service templates** if a future redeploy should stay on `yynuu` — confirm `spec.template` env on both services matches the serving revisions, so the next `--source` deploy doesn't silently revert DB/keys.
- [ ] **Stray `backend/src/routes/sync.js`** sits next to `sync.ts` — harmless to this change but worth removing for clarity.
- [ ] (Optional) **Duplicate prevention** — if you want the check to *block* (not just flag), gate `post_to_push_queue` on `invoice_exists` and/or also check `push_queue`, with normalized matching.
- [ ] (Optional) **Sale-side dedupe** — no natural key today; would need party+date+item-hash or a challan number.

---

## 9. Quick command reference

```bash
# What's serving + which DB (per service)
gcloud run services describe tallybridge-backend --region=asia-south1 --format="value(status.traffic)"
gcloud run revisions  describe <REV> --region=asia-south1 --format="value(spec.containers[0].env)"   # ACTUAL running env

# Deploy from source (creates new revision; may land at 0% if traffic is pinned)
gcloud run deploy tallybridge-backend --source backend --region asia-south1 --project tallybridge-test-ocr
gcloud run deploy tallybridge-parsing --source parsing --region asia-south1 --project tallybridge-test-ocr \
  --min-instances=1 --memory=2Gi --cpu=2 --timeout=3600

# Promote a revision to 100% (REQUIRED after deploy when traffic is pinned)
gcloud run services update-traffic tallybridge-backend --region asia-south1 --to-revisions <REV>=100
gcloud run services update-traffic tallybridge-parsing --region asia-south1 --to-latest

# Live test (⚠️ pushes a real voucher into the queue → may reach client Tally if a poller is active)
curl -s -X POST "https://tallybridge-parsing-…run.app/docstrange?purchase=all&source=runpod&company=K+V+ENTERPRISES" \
  -H "Content-Type: application/pdf" --data-binary "@<file.pdf>"
```

### Key files
| File | Purpose |
|---|---|
| `parsing/server/handler.py` | OCR server; `check_duplicacy:1640`; `invoice_exists` injection in `/docstrange` branch |
| `backend/src/routes/sync.ts` | `/push-queue` insert (`:2238`), `normalizePushVoucherPayload` (`invoice_exists` whitelist), `push_now` GET filter (`:2410`), `/activate` (`:2309`) |
| `src/main/push-queue-poller.ts` | desktop poller → `sync_main.py (poll_push_queue)` |
| `src/python/cloud_pusher.py` | `fetch_pending_push_vouchers()` → GET `/push-queue` |
| `backend/full_schema.sql` / `supabase_new_tables.sql` | `push_queue` def: `status DEFAULT 'pending' CHECK(pending|push_now|pushed|failed)`, no trigger |

# Handout — Why the client's push_queue insert pushed to Tally *immediately* (backend version skew)

> Continuation handout for a fresh chat. Covers the investigation since the last
> compact: the user reported that on the client (app **1.2.8**), **inserting a row
> into the Supabase `push_queue` immediately pushed it to TallyPrime — with no one
> pressing "Push Now."** This document explains the real cause, corrects the
> architecture mental-model, and lists the implications for un-pausing.

---

## 0. TL;DR

- The desktop app does **NOT** read Supabase directly. It polls the **Express backend** (`/api/sync/push-queue`); the **backend** is the only thing that touches Supabase.
- The `pending → activate → push_now` safety gate is a **backend code feature**, added in commit **`feeb928` (2026-05-20)**, which is **only on `TallyBridge-Backend-Refactor`, NOT on `main`.**
- The client's **deployed backend** is the **older** version (`3af32a4`, 2026-04-27), where:
  - enqueue → `status:"pending"`, and
  - `GET /push-queue` returns `status="pending"` rows **directly**.
- So on the client's backend there is **no activation step**: any insert is served to the poller within ~5 s and imported into live Tally. A human "Push Now" was **never required**.
- Most likely trigger of the balaji bill: the **GCP OCR parsing service** called `post_to_push_queue()` while pointed at the client's backend/Supabase → enqueued `pending` → poller pushed it to Tally (`CREATED=1`, `ADDISON` stock-item line error).

---

## 1. The corrected architecture (this was the key misunderstanding)

**Wrong model (what the user believed):**
> Backend writes to Supabase `push_queue` → status turns to `push_now` → TallyBridge
> desktop *watches Supabase*, detects it, and pushes to TallyPrime.

**Actual model:**

```
Supabase push_queue table
        ▲  reads / writes
        │
   Express backend (Cloud Run, tallybridge-backend-*, port 3001)
        │   GET /api/sync/push-queue   ← decides which rows to return (status filter lives HERE)
        ▲
        │  HTTP poll every ~5s   (cloud_pusher.py:352 → {BACKEND_URL}/api/sync/push-queue)
        │
   Desktop TallyBridge poller (push-queue-poller.ts, 5s timer)
        │   spawns python  TB_COMMAND=poll_push_queue
        ▼   push_vouchers()
   TallyPrime :9000
```

Two hops: **Supabase ⇄ backend ⇄ desktop**, not one. The desktop app:
- never queries Supabase for the queue,
- never sees a row's `status`,
- just imports whatever rows the backend returns.

So "TallyBridge detects push_now" is the **new backend's** design. It is *not* what the client runs.

---

## 2. The smoking gun (git evidence)

| Item | Commit | Date | On `main`? | On refactor branch? |
|---|---|---|---|---|
| Push queue poller + enqueue=`pending`, GET filters `pending` | `3af32a4` | 2026-04-27 | yes (base) | yes |
| Two-step gate: `push_now`, `/push-queue/activate`, GET filters `push_now` | `feeb928` | 2026-05-20 | **NO** | yes (HEAD) |

Verified:
- `git merge-base --is-ancestor feeb928 main` → **feeb928 NOT in main**
- `git merge-base --is-ancestor feeb928 HEAD` → feeb928 IS in HEAD (refactor branch)
- branches containing `feeb928`: `TallyBridge-Backend-Refactor`, `backup-local-restructure`, `gcp-live-backup`

### Old backend `3af32a4` (what the client runs)
```
POST /push-queue   →  insert { status: "pending" }     (sync.ts:1872 @ 3af32a4)
GET  /push-queue   →  .eq("status", "pending")          (sync.ts:1912 @ 3af32a4)
```
`pending` is both the birth status AND the served status → **immediate push, no gate.**

### New backend `feeb928` / refactor HEAD (what is NOT deployed)
```
POST /push-queue            →  insert { status: "pending" }      (sync.ts:2289)
POST /push-queue/activate   →  pending → "push_now"              (sync.ts:2354-2367; requires current status "pending")
GET  /push-queue            →  .eq("status", "push_now")         (sync.ts:2410)
```
Here `pending` rows are invisible to the poller until something activates them.

---

## 3. How the balaji bill reached live Tally with "no one pressing push"

On the old backend, no button press is needed. The trigger was an **insert**, and the most probable inserter is the **GCP OCR parsing service**, not a human:

```
parsing handler  →  post_to_push_queue()                 (parsing/server/handler.py:1012)
                 →  POST {PUSH_QUEUE_URL}/api/sync/push-queue   (handler.py:91-100; defaults to K V ENTERPRISES)
                 →  backend inserts status:"pending"
                 →  desktop poller GET sees "pending"   →  push_vouchers()  →  Tally
                                                            (CREATED=1, LINEERROR "Stock Item 'ADDISON' does not exist!")
```

So whoever ran a sale PDF through the parsing endpoint **while it was pointed at the client's backend/Supabase** effectively pushed straight into the client's live Tally. Earlier (pre-compact) we had hypothesized a manual "Push Now"; the git evidence shows a manual press was **not** required at all. Either path (desktop "Push Now" OR OCR enqueue) lands as `pending` and is pushed immediately on the old backend.

---

## 4. Why app version (1.2.8) is irrelevant here

The status gating is 100% **backend code**. The desktop app only relays the backend's filtered list. Updating/downgrading the desktop app cannot add or remove the `push_now` gate — only deploying the `feeb928` backend can.

---

## 5. Implications for the resume / un-pause plan

The earlier resume plan (see `session_handout_supabase_pause_resume.md`) said: ship incremental app update → restore the stubbed RPCs. **Add this:**

- The `push_now` activation gate **only protects the client once the `feeb928` backend is deployed** to their Cloud Run service.
- Until that backend ships, **anything** inserted into the client's `push_queue` (OCR parsing run, a test enqueue, a stray POST) goes to live Tally automatically within ~5 s.
- Therefore: **do not run any sale/purchase PDF through the parsing service while it points at the client backend/Supabase**, and treat the client `push_queue` as "live-armed" until the new backend is deployed.

### Safe sequence to actually close this out
1. Deploy the **`feeb928`** (refactor) backend to the client's Cloud Run so the `push_now` gate becomes active. (Verify the GET filter is `push_now` after deploy.)
2. Then the queue is safe: inserts sit as `pending` until explicitly activated.
3. Proceed with the rest of the resume plan (incremental sync app update, restore stubbed RPCs).

---

## 6. Open / unverified

- **Not directly confirmed:** which exact commit the client's deployed backend Cloud Run revision was built from (reading deployed Cloud Run config is gated). The code evidence is unambiguous that it is **pre-`feeb928`**, but a definitive check = inspect the deployed revision / its source commit (`gcloud run revisions describe ...`, user-run).
- The errant `SALE-20260608095548` (BALAJI) voucher is partially in the client's live Tally (failed on the ADDISON stock item). Client still needs to review/delete it.

---

## 7. Key files / locations

| Item | Location |
|---|---|
| Desktop poller (calls backend, not Supabase) | `src/main/push-queue-poller.ts` (5s timer, spawns `TB_COMMAND=poll_push_queue`) |
| Desktop fetch → backend URL | `src/python/cloud_pusher.py:352` (`GET {BACKEND_URL}/api/sync/push-queue`) |
| Push cycle / import to Tally | `src/python/sync_main.py:1108` (`run_pending_push_cycle`) → `tally_pusher.push_vouchers` |
| Backend queue routes (NEW, gated) | `backend/src/routes/sync.ts` — enqueue 2289, activate 2354-2367, GET 2410 |
| Backend queue routes (OLD, client) | `git show 3af32a4:backend/src/routes/sync.ts` — enqueue 1872, GET filter 1912 |
| OCR parsing enqueue | `parsing/server/handler.py:1012` (`post_to_push_queue`), URL config 91-100 |
| The fix commit | `feeb928` (2026-05-20) — only on refactor branches, NOT `main` |

---

## 8. One-line summary for the next chat

> The client's deployed **backend** is the pre-`feeb928` version where `GET /push-queue`
> serves `status="pending"` rows straight to the desktop poller — so any insert
> (likely the GCP OCR parsing service) pushes to live Tally within ~5 s with no
> activation. The `push_now` gate exists only on the unshipped refactor branch.
> Deploy `feeb928` backend before treating the client queue as safe.

# TallyBridge — Party Matcher: Retrieval Truncation & Abbreviation Expansion

Context-transfer doc. Continues `session_handout_2026-08-03_sale_pricing_balance_and_party_matcher.md`,
which left workstream 4 half-finished.

---

## 0. TL;DR

| # | Item | State |
|---|---|---|
| 1 | `get_distinct_party_names` SQL on **testing** | ✅ applied (had been done between sessions) |
| 2 | **RPC returned only 1000 of 1376 names** — new bug, found and fixed | ✅ committed `8720c6f` |
| 3 | `P. T ENT.` abbreviation expansion | ✅ committed `5b95a95` |
| 4 | `tallybridge-parsing` on testing | ✅ `00019-ktv`, verified byte-identical to HEAD |
| 5 | `get_distinct_party_names` SQL on **client** | ❌ **pending — `PGRST202` today** |
| 6 | Parsing on client | ❌ not deployed |

Branch `update/absolute-latest-rate-etc`, 2 commits, **not pushed** (deliberate).

---

## 1. The handout's fix was incomplete — and looked correct

The previous session's step 2 said *"Expect ~1376"*. That was a prediction, never observed.
The actual result:

```
POST /rest/v1/rpc/get_distinct_party_names
  -> HTTP 206   Content-Range: 0-999/1376
  first: 3S DESIGN        last: S K ENTERPRISES (MANESAR)
```

**PostgREST caps a response at its own max-rows (1000).** So the migration fixed the *ratio*
(6000 invoices → 1376 customers) but not the *ceiling*: everything from `S LAL TOOLS` to
`ZODIAC ENGINEERS` — 376 customers, 27% — was still unreachable. On the client it is worse:
1490 distinct parties, so ~490 would be dropped.

Same bug class as the one being fixed, one level up, and equally silent. `MUNDHARA AGENCIES`
happens to sit inside the first 1000, so the original verification would have passed while a
quarter of the customer base stayed broken.

### Two traps in paging it

**1. PostgREST ignores `Range` headers on an RPC `POST`.**

```
Range: 1000-1999  ->  200, Content-Range 0-999/*, first row '3S DESIGN'   <- page one again
?limit=1000&offset=1000 ->  200, Content-Range 1000-1375/*, first 'S LAL TOOLS'  <- correct
```

A Range-based loop re-fetches rows 0-999 forever while reporting success. It reads as working.

**2. A short page means "capped", not "done".**

PostgREST truncates a page to max-rows regardless of the `limit` asked for. So
`len(rows) < page_size` is *not* an end-of-data signal when a total is known. The loop asks for
`Prefer: count=exact` on the first page only and pages toward that total; short-page detection
is the fallback for when no total comes back.

### Verified on testing

```
1376 names, '3S DESIGN' .. 'ZODIAC ENGINEERS', 0 duplicates
376 names past the old cut
MUNDHRA AGENCIES -> MUNDHARA AGENCIES, score 122.87   (was 211 names / no match / 31.85)
```

And end-to-end through the deployed service, replaying the original failing image
(`?type=sale`, **no** `&push=queue`):

```
HTTP 200, 194s
ocr_text     : "MUNDHRA AGENCIES"
matched_name : "MUNDHARA AGENCIES"      <- was ""
score        : 122.87                   <- was 31.85
candidates   : MUNDHARA AGENCIES 122.87 · CPM AGENCIES 32.64 · Industrial Sales Agency 32.31
```

Previously every candidate scored was an A/B name. Side effects checked and none found:
newest `push_queue` row predates the replay by 4.5h, and `build_gsheet_rows` only *builds* a
payload for n8n — the handler makes no Sheets API call, so calling it directly writes nothing.

### The fallback is still load-bearing

`fetch_distinct_party_names()` returns `None` on any first-page failure, kept deliberately
distinct from `[]`. The client returns `PGRST202` **right now**, so it is running on
`_party_candidates_by_paging` today. A 404 that wiped the candidate list would turn every scan
into a garbage invoice.

---

## 2. `P. T ENT.` — abbreviation expansion

Rejected at 54.56 against a 78 threshold despite being a 90% string match for
`P. T. ENTERPRISES`. `strong_party_tokens` drops tokens under 3 characters, so `P` and `T`
vanish and only the generic `ENTERPRISES` survives — nothing identifying is left to score on.

Added to `normalize_party_name`, reusing the existing `HARDW -> HARDWARE` idiom:
`ENT`/`ENTP`/`ENTR` → `ENTERPRISES`, `AGY` → `AGENCY`, `CORP` → `CORPORATION`.

**Measured against the client's 1,490 real party names.** 246 can change behaviour at all; the
other 1,244 normalise identically and therefore score identically:

| | clean reads | abbreviated reads |
|---|---|---|
| baseline | 246/246, 0 mis-attributed | 216/246, 1 mis-attributed |
| applied | 246/246, 0 mis-attributed | **245/246**, 1 mis-attributed |

+30 recovered. **Nothing newly mis-attributed on a clean read.**

`CORP` is not speculative — the client ledger holds `BHARAT SALES CORP.`, `HSN TRADING CORP.`,
`MILAN SALES CORP (NIT)` and 32 more. A full read of `BHARAT SALES CORPORATION` now scores
120.0 against them, up from 98.05.

### The one loss, and the trap in "fixing" it

`SUNRISE ENTERPRISE` regresses. It is **not** fixable: the client has **both**
`SUNRISE ENTERPRISE` and `SUNRISE ENTERPRISES`, and abbreviating to `SUNRISE ENT.` erases the
only character separating them. Baseline resolved it correctly by luck; this resolves it to the
other one by luck.

Collapsing singular into plural was tried and **rejected** — it makes a *perfectly read*
`SUNRISE ENTERPRISES` resolve to `SUNRISE ENTERPRISE`, breaking a clean read to patch an
unresolvable one. `test_party_normalization.py` pins that decision so it is not "fixed" later.

---

## 3. Open: a single generic word matches a random customer

Measured, **not fixed** — this is the INT-06 family and it is worse than the handout recorded.

```
'SALES'       -> JAYA SALES      113.53   strong_ratio 1.0  (accepted by score AND the >=0.99 bypass)
'AGENCIES'    -> CPM AGENCIES     80.20   strong_ratio 0.0  (accepted by score)
'TOOLS'       -> RG TOOLS         80.35
'ENTERPRISES' -> B ENTERPRISES    83.43
'TRADERS'     -> Ab Traders       82.69
```

Two distinct mechanisms. `SALES` is **not** in `PARTY_GENERIC_TOKENS`, so it counts as a strong
token and hits the `strong_ratio >= 0.99` bypass. The others **are** generic, so `strong_tokens`
is empty, both penalties gate off (`:444-445`), and raw fuzzy clears 78 unaided.

Either way a partial read files an invoice against an arbitrary customer — a *wrong* invoice,
not a garbage one, so nothing surfaces it.

### A third option, measured

The previous session measured two fixes and rejected both. There is a third:

| guard | fixes it | cost |
|---|---|---|
| add `SALES` to `PARTY_GENERIC_TOKENS` | ✗ no change | — |
| require a strong token | ✓ | rejects **63** customers |
| **reject probes that are *entirely* generic** | ✓ all 7 probes | rejects **2** of 1490 |

The difference is the length filter. `strong_party_tokens` drops tokens under 3 characters, so
`A K TOOLS` has no strong tokens and a strong-token rule kills it. Asking instead "is *anything*
here non-generic", with no length filter, keeps every initial-style name. The two casualties are
`INDUSTRIAL TRADING CORPORATION` and `Industrial Sales Agency` — genuinely all-generic names.

Not implemented. Needs a decision.

---

## 4. Deployment state

| component | testing | client |
|---|---|---|
| Supabase `get_distinct_party_names` | ✅ applied | ❌ **pending (`PGRST202`)** |
| `tallybridge-parsing` | ✅ `00019-ktv` | ❌ `00012-2lx` (old) |
| `tallybridge-backend` | `00013-2wj` | `00005-g4z` |
| Flutter app | ❌ never deployed | ❌ never deployed |

Deploy verified the recommended way: build archive downloaded from
`run-sources-tally-bridge-testing-env-asia-south1` and diffed against HEAD — `handler.py`
byte-identical.

### To finish on the client

1. Paste `supabase/migrations/20260803_distinct_party_names.sql` into the **client** (`ztugw`)
   SQL Editor. Safe in either order — without it the code falls back, with it and old code
   nothing calls the RPC.
2. `gcloud config configurations activate deployment-riplara`
3. `gcloud run deploy tallybridge-parsing --source parsing --region asia-south1 --project tally-bridge-deployment-env`

---

## 5. Gotchas found this session

**`gcloud builds list` needs `--region asia-south1`.** Without it the command returns nothing at
all for these projects — not an error, just empty.

**Two Cloud Run URL forms both point at testing.** `tallybridge-parsing-828647628834.asia-south1.run.app`
(project-number form) and `tallybridge-parsing-5lj6vlxoma-el.a.run.app` are the same service.
`828647628834` **is** the testing project number — confirmed, not assumed.

**gcloud was left on `deployment-riplara` (the client).** Each project only accepts its own
account, so a wrong-project deploy fails loudly rather than silently — but check before
deploying. It is now on `testing-riplara`.

**`PARTY_NAME_CACHE` never expires.** It is a module global filled on first use and held for the
container's lifetime, so a customer created in Tally today cannot be matched until the instance
recycles. Pre-existing, not introduced here.

**`python3` piped to `tail` buffers everything until exit** — background sweeps show an empty
output file the whole time they run. Use `-u` and avoid `| tail` if you want progress.

---

## 6. Tests added

`parsing/server/test_party_candidates.py` (30 checks) — retrieval: querystring-not-Range,
paging to the exact total, server-capped pages, page-budget guard, `None` vs `[]` degradation,
RPC-vs-fallback route selection, route parity. Mutation-checked: reverting the paging loop to a
single request fails exactly `collects every page` and `stops as soon as the total is consumed`.

`parsing/server/test_party_normalization.py` (38 checks) — expansions, probe/ledger convergence,
no firing inside longer words (`ENGINEERING`, `CORPORATE`, `ENTRY`), pre-existing rules intact,
and the deliberate singular/plural non-collapse.

Both are standalone scripts in the existing style: `python server/test_*.py`, no network.
All four parsing suites pass.

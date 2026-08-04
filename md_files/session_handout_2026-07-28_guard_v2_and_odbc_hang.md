# Session Handout — RCA Verification, Guard v2 Rollout, and the ODBC Hang

**Date:** 2026-07-28
**Scope:** (1) empirical verification of the 2026-07-27 stock-unit RCA, (2) design, testing and
production rollout of a hardened `stock_items` guard, (3) root cause of the 3-day client sync
outage discovered along the way.
**Environments touched:** yynuu (testing, read/write on throwaway rows), ztugw (client production —
one DDL apply plus a self-cleaning smoke test), BetterStack `tallybridge-client` (read-only),
local macOS (Darwin 25.5.0, arm64).
**Predecessor:** `session_handout_2026-07-27_stock_unit_corruption_rca.md` — referred to throughout
as "the RCA doc".

---

## 1. Executive summary

Three outcomes.

**The RCA's causal chain holds, and was reproduced end-to-end on macOS.** A mock TallyPrime on
`127.0.0.1:9111` drove the real, unmodified `sync_main.fetch_stock_xml()` → `cloud_pusher` →
Supabase, and landed `unit='Nos', group_name=NULL` exactly as the doc describes. No Windows
machine was required for any part of the chain that determines the fix.

**Two of the RCA's claims are wrong** and should be corrected in place: §3.2's payload byte-delta
evidence is statistical noise, and §7.1 #4's stated mechanism for the stranded junk rows is a
non-sequitur. Several other claims are directionally right but materially imprecise — most
consequentially, the fabrication sites number **five, not two**.

**The client's sync has been dead since 2026-07-27 02:43 UTC, and the cause is a bug in our own
code**: `odbc_bridge.probe()` contains **three unguarded blocking calls**, any of which turns a
wedged ODBC helper into a permanent, silent hang. The hang's *location* is established by evidence;
*which* of the three calls blocked is not (§8.3). The same subsystem — degrading differently — is
what forced the XML fallback that produced the original corruption on 07-24. One unhealthy
component, two incidents.

**Separately: nothing at all reached BetterStack on 2026-07-28.** That is not the same as the
07-27 hang and must not be read as such — see §8.1.

A hardened guard (**v2**) is now live on both yynuu and ztugw, verified by behavioural test in
both. It exists in no repository; that remains open.

---

## 2. What was verified, and how

A 40-agent workflow tested **14 independent claims** drawn from the RCA doc. Each claim was
tested empirically, then adversarially reviewed through three lenses (fixture fidelity, logical
sufficiency, hidden confound). Run stats: 580,708 subagent tokens, 160 tool uses, 0 agent errors.

| # | Claim | Result |
|---|---|---|
| C1 | `xml_parser.py:523` fabricates `"Nos"` on absent `BASEUNITS` | CONFIRMED |
| C2 | Definition-driven path fabricates via `"default": "Nos"` | CONFIRMED |
| C3 | Healthy XML round-trips untouched (the control) | CONFIRMED |
| C4 | ODBC definition has no default → `NULL`, never `'Nos'` | CONFIRMED |
| C5 | Voucher inventory lines fabricate too | CONFIRMED |
| C6 | `NULLIF(TRIM(unit),'')` — the DB rendered damage visible | CONFIRMED |
| C7 | Guard case 1 — degraded update rejected | CONFIRMED |
| C8 | Guard cases 2+3 — legit change applies, freeze is surgical | CONFIRMED |
| C9 | Guard case 4 — the INSERT gap is real | CONFIRMED |
| C10 | §7.1 #4 — "the guard freezes the 22 junk rows" | **REFUTED** |
| C11 | Full chain reproduces on macOS via mock Tally | CONFIRMED |
| C12 | §3.2 — payload byte-delta proves a missing group name | **REFUTED** |
| C13 | §7.3 repo-drift claims | PARTIAL |
| C14 | What genuinely needs Windows | PARTIAL |

No finding was overturned by a reviewer. `QUALIFIED` dominated the review verdicts — reviewers
consistently narrowed claims rather than rubber-stamping them, which is the intended behaviour.

### 2.1 Reproduced end-to-end on macOS

A stdlib HTTP server on `127.0.0.1:9111` served degraded `STOCKITEM` elements (name present,
`PARENT`/`BASEUNITS` absent) to the **real, unmodified** engine. `TALLY_URL` is a bare env var
(`tally_client.py:10`), so nothing in the read path can tell the mock from TallyPrime. It printed
the exact log lines quoted in RCA §3.2 (`[Tally] Stock items loaded via definition-driven
collection`), landed 5 rows as `unit='Nos', group_name=NULL`, and then **repaired** to
`PAIR/TOTEM`, `NOS/ADDISON`, `BOTTLE/PIDILITE` when the mock was flipped to healthy — reproducing
the doc's "11:17 ⇒ master repaired" transition locally.

### 2.2 A narrowing the RCA does not state

The 07-24 read used the **definition-driven** path, whose unit sources are
`[BASEUNITS, CLOSINGBALANCE, STKCLOSINGBALANCE, STKCLBALANCE, CLOSINGQTY]`. Verified: a degraded
response that *kept* a unit-bearing `CLOSINGBALANCE` (e.g. `"5 PAIR"`) recovers `PAIR` and causes
no corruption at all. So the true degraded shape must have lost `PARENT`, `BASEUNITS` **and every
quantity field's unit token** — genuinely name-only objects. That is a stronger and more specific
claim than §3.2 makes.

---

## 3. Where the RCA doc is wrong — correct these

Ordered by consequence.

### 3.1 §3.2's byte-delta evidence is noise. **Delete the paragraph.**

The doc argues: *"the same 14,459 rows are 1,881,045 bytes via XML vs 2,003,151 via ODBC — ~122KB
lighter, ≈8 bytes/row, consistent with a missing group name per row."*

The arithmetic is right (`122,106 / 14,459 = 8.44`) and the inference is worthless:

- **`bytes=` is not a wire size.** `estimate_payload_bytes()` (`sync_main.py:1980`) serialises only
  the **first `_BYTES_SAMPLE_SIZE = 200` rows** (constant at `:1977`) of 14,459 and extrapolates:
  `per_row = (sample_bytes - 2) / len(sample)`. Deliberately observability-only, to avoid a
  full-serialisation OOM.
- XML and ODBC return rows in **different, unspecified orders**, so the two numbers extrapolate
  from two non-comparable 200-row samples.
- **Measured noise floor:** sliding which 200 rows are sampled — over *identical, unmodified*
  production rows — swings the reported total by **415,624 bytes (20.34%)**. The claimed signal is
  122,106 bytes (6.1%). The noise is **3.4× larger than the effect**.
- The number doesn't fit the mechanism either: blanking `group_name` costs **5.05 B/row** on real
  data; omitting the key entirely costs **21.05 B/row**. 8.44 matches neither — and `_parse_row`
  never omits a key, so the ~21 B/row mechanism is impossible in this codebase.

This does not touch §3.1 or §3.3, which stand on independent evidence. But it means **the 18/18
`synced_at` correlation (§3.3) is now carrying weight the byte delta was falsely carrying**, and
it was never re-verified in this session — it needs a read against ztugw.

### 3.2 §7.1 #4's mechanism is backwards. **REFUTED.**

The doc says the guard *"now freezes [the 22 junk rows] at `Nos` (since `OLD.unit` is already
bad)"*. It does not. A row in the exact junk state (`unit='Nos'`, `group_name=NULL`) that receives
a healthy payload **repairs completely** — the `WHEN` clause evaluates false and the trigger body
never runs. §6 of the same doc says exactly this ("on a healthy sync the function is never
entered"); §7.1 contradicts it. Two independent reviewers reproduced the repair, after finding
the original test's control degenerate.

**The conclusion survives for the doc's own §4 reason**, not the reason §7.1 gives: the 3 Group-B
rows carry mangled `?` names so `ON CONFLICT (company_id, name)` never matches, and the 19 Group-A
rows are brand names Tally never re-sends. They are stranded by **key mismatch**, not by the guard.

> **Practical consequence, and it's the opposite of what the doc implies:** you do **not** need to
> drop or work around the guard to repair those rows. A plain `UPDATE` setting a real unit and a
> non-null group passes the `WHEN` clause untouched. Verified live.

### 3.3 §3.5 attributes the bug to one line. It is **five sites, plus three in the backend.**

| Site | In the doc? | Notes |
|---|---|---|
| `xml_parser.py:351` | ✅ | voucher inventory lines |
| `xml_parser.py:523` | ✅ | stock master, xmltodict branch |
| `xml_parser.py:550` | ⚠️ **misattributed** | doc calls it a voucher line. It is inside `parse_stock()` (def at line 489) — a **third stock-master fabricator**, in the Stock Summary regex fallback. It hardcodes `group_name ""` *and* `unit "Nos"`, i.e. it produces the exact 22-row fingerprint from a **perfectly healthy** Tally |
| `structured_sections.json:529` | ❌ **not mentioned** | vouchers→items→unit |
| `structured_sections.json:931` | ✅ | doc says "around 927"; 927 is the `STKCLOSINGBALANCE` source entry |
| `backend/src/routes/sync.ts:265, 355, 2641` | ❌ | uppercase `|| "NOS"` — see §3.6 |

§7.2 #6 as written ("drop `or \"Nos\"` at `:523` and `\"default\": \"Nos\"` (singular)") would leave
**four fabricators live**.

### 3.4 §3.5's `group_name` mechanism is wrong at the Python layer

The doc writes `"group_name": { "sources": ["PARENT"] } // no default → NULL`. The parser emits
`''` (a `str`), never `None`. The `NULL` happens one layer later, via `NULLIF(TRIM(...),'')` in
Postgres.

> **This matters directly for §7.2 #7.** A degradation detector written in Python as
> `if row["group_name"] is None` would **never fire**. Test against `""`.

### 3.5 §3.6's "the database was doing the right thing" is about visibility, not protection

The same statement is `ON CONFLICT DO UPDATE SET unit = EXCLUDED.unit` with no value guard. A
degraded read **destroys** a correct `NOS`; it does not merely fail to fill a gap. The honest
counterfactual: *the good data would still have been wrecked; the wreckage would have been an
obvious `NULL` instead of a plausible `'Nos'`*. And "visible" is by convention only — `unit` has
no `NOT NULL` constraint, so nothing would have raised. Note the doc's own §4 undercuts the point:
`group_name` **did** degrade to `NULL` on those rows, exactly as the doc says would have been
noticed — and it wasn't, for three days.

### 3.6 Stale citations (all pre-date the doc; not drift since)

- `parsing/server/handler.py`: `:124` ✅ and `:567` ✅ exact at HEAD. `:1312` is actually **1327**;
  `:1647` is actually **1662** — a consistent **+15** offset. The file is unchanged since commit
  `53014c9` (2026-07-24), so these were already off when the doc was written. `:1674` matches.
- §3.6 cites `20260427…:142` / `20260619…:151`; in the tracked file the `INSERT` begins at **150**
  and `ON CONFLICT` at **174**. Substance correct, offsets stale.
- §8's env table says `deployment → .env_clinet`. On this machine the file is `.env.deployment`.

---

## 4. New findings not in the RCA at all

**About the parser**

1. **The definition-driven path fabricates identically.** Output is byte-identical to
   `parse_stock()` on the same fixture across all 9 rows. Fixing `:523` alone leaves the
   production path bleeding — `sync_main.py:1464` tries `fetch_structured_section("stock_items")`
   **first**; `parse_stock` is the fallback.
2. **The `CLOSINGBALANCE` unit-fallback is dead code in the shape that matters.**
   `definition_extractor._is_empty()` treats a dict as empty only when `len == 0`, so
   `<BASEUNITS TYPE="String"></BASEUNITS>` — the shape real Tally emits, with `TYPE` on every
   child — is *truthy*, wins the source race, stringifies to `''`, and falls to `"Nos"`. The
   `CLOSINGBALANCE` rescue only fires when the tag is absent entirely or carries no attribute,
   i.e. **never in production**. Fixing `_is_empty` to ignore attribute-only dicts is an
   independent hardening.
3. **`_quantity_unit()` silently truncates multi-word units to their second word.**
   `BASEUNITS="Not Applicable"` → **`'Applicable'`**. RCA §4 records 2 ztugw rows where Tally
   says `Not Applicable`; via the XML path those are stored corrupted. Separately,
   `parsing/server/handler.py:610` maps `"NOS" → "Nos"` — downcasing a legitimate master unit into
   exactly the title-case pattern the RCA is chasing.

**About the guard — it is much narrower than §6 implies**

4. **v1 was exactly one 3-byte string wide.** `unit='NOS'` and `unit='nos'` both sailed through
   and overwrote a `PAIR` row. Good news: it did not over-freeze Tally's legitimate uppercase
   `NOS`. Bad news: one casing change in the parser default and the guard becomes a silent no-op
   while still reading "enabled" in both dashboards.
5. **v1's `group_name` half was bypassed by an empty string** — precisely what `xml_parser.py:522`
   emits and what render-mode ingest stores unnormalised. `NEW.group_name IS NULL` is false for `''`.
6. **v1 was bypassed by whitespace.** A direct PostgREST `PATCH` (no `TRIM`) wrote `'  Nos  '`
   straight over `'PAIR'`. Demonstrated, not theorised.
7. **v1 protected only two columns.** In the mock end-to-end run, a degraded replay over a healthy
   master held `PAIR`/`TOTEM` but still set **`closing_qty 4.0 → 0.0`** and
   **`part_code 'ZZ11-PC-PAIR' → NULL`** on every row. *A degraded read still corrupted stock
   quantities and part codes.* This is the strongest argument for §7.2 #7 and is stated nowhere in
   the RCA.
8. **The guard is silent.** Degraded calls return `HTTP 200 {"stock": 1}`. A caller cannot tell
   its write was rejected.

**About the INSERT gap**

9. **A wrongly-inserted row self-heals.** The doc's Case 4 ⚠️ reads as a permanent hole; it is a
   **one-sync exposure window**. The first healthy sync including that item repairs both fields.
   Verified twice, and healing is per-field.
10. **But the window is event-bounded, not time-bounded.** Four consecutive degraded syncs left the
    row at `'Nos'`/NULL — the guard fires and copies `OLD.unit`, which is itself `'Nos'`,
    self-healing to the wrong value indefinitely. Transient in principle; permanent in practice
    while the read bug persists.
11. **The parser fix does more than add detectability, and this is why it must stay high in the
    order.** One verification argued that since a `NULL` unit inserts as freely as `'Nos'`, §7.2 #6
    "buys detectability, nothing more." Both reviewers overturned it, and the code agrees:
    `backend/src/routes/sync.ts:262-264` **rejects** a voucher item with a missing unit
    (`"voucher_payload.items[…].unit is required"`) — a `NULL` **fails closed** at the voucher
    boundary while `'Nos'` sails through as a valid string. And `push-invoice.ts:103`
    (`item.unit || "NOS"`) coerces a NULL to the correct majority unit while freezing a truthy
    `'Nos'` into `push_queue` verbatim.
12. **A third `stock_items` writer exists that §6's "confirmed non-interference" misses.**
    `backend/src/routes/sync.ts:2629` `seedCreatedStockItem` upserts into `stock_items` on
    push-ack with `unit: normalizeTrimmedString(payload.unit) || "NOS"`. §6's claim that "a newly
    created item reaches `stock_items` only via the next sync" is wrong at the backend layer. It
    uses `ignoreDuplicates: true` (INSERT-only), so it can seed a wrong unit for a new item but
    cannot corrupt an existing row.

**Ingest-mode conditionality, absent from the RCA entirely**

13. Everything verified on the DB side used the **hybrid/direct** RPC. In `render` mode
    `backend/src/routes/sync.ts:2137-2139` upserts the parser row **verbatim** — no `NULLIF`, no
    `TRIM` (`stock_items.map(s => ({ ...s, … }))` straight into `upsertInBatches`).
    A degraded `group_name` then persists as `''`, which (a) is invisible to every `IS NULL` check
    the visibility argument depends on, and (b) **does not arm the guard's null predicate**.
    `store.ts:78` defaults to hybrid and `index.ts:109` force-migrates render→hybrid, and the
    client's own BetterStack logs confirm `ingest_mode=hybrid` — so this is a tail risk for
    fan-out, not a live one for ztugw. `cloud_pusher.py:57` still falls back to `"render"`.

**Repo drift**

14. **The guard exists nowhere in this repository.** Exhaustively checked: working tree grep,
    `git log --all -S`, `git grep` over every commit, `git stash list`, `git reflog --all`,
    `git fsck --full --unreachable --dangling` with `cat-file -p` on every dangling object,
    `~/.Trash`, and a second clone at `~/.codex/worktrees/335c/TallyBridge`. **Zero hits.**
15. **The two untracked files §7.3 #9 describes are gone.** `supabase/migrations/` holds 6 files,
    all dated 1 Jul. The live DDL now exists only inside two Supabase dashboards.
16. **`.claude/skills/` does not exist** at project or user level; `check_units.js` is nowhere
    under `~`. The `voucher-unit-check` tool §8 describes **cannot be run**.
17. All fabrication sites remain unfixed. The only commit since 2026-07-27 is `8f20809`, docs-only.

---

## 5. macOS vs Windows — the boundary

**~80% of the doc's technical content was testable on macOS, and 100% of the part that determines
the fix.**

Everything about *our code* — the fabrication, the `group_name` asymmetry, the ODBC contrast, the
SQL overwrite, all four guard cases, the fix and its regression test — runs here. The Python
engine imports cleanly on Darwin, and `sync-engine.ts:353-356` deliberately falls back to `python3`
on non-`win32`.

**Genuinely Windows-only:**

| Item | Needs | Client machine specifically? |
|---|---|---|
| RCA §5 Q13 — why the 07-24 read lost `PARENT`/`BASEUNITS` | Any TallyPrime | **Largely answered — see §7** |
| §5 H3 (4 FETCH variants), unit-master enumeration | Any TallyPrime | No |
| Live ODBC read path | Windows **guest**, engine running *inside* it | No |
| That machine's ODBC wedge | — | **Yes** |
| §7.4 #12 — which company is loaded (13,250 / 14,459 / 17,246) | — | **Yes** |
| TallyPrime release version (`Help > About`) — never recorded | — | **Yes.** One screenshot; should be step zero |

`odbc_bridge.available()` returns `os.name == "nt" and …`; on macOS `_powershell_candidates()`
returns `[]`. Wine does not help — macOS-native Python still reports `posix`. This is an **arm64**
Mac, so a Windows VM would be Windows-on-ARM emulating x64; fine for a semantic yes/no test, not
for anything timing-sensitive.

### The honest epistemic gap

**Nobody has ever seen the XML TallyPrime returned at 11:06 on 2026-07-24.** No capture exists, and
the repo contains no real `StockItem` collection response at all —
`src/python/tally-responses/05_stock_items_master.xml` is a `<LINEERROR>`. The mock proves
**IF degraded THEN `Nos`**. It does not prove **Tally degraded**. Those bytes are gone permanently;
a Windows box can tell you whether it can be made to happen *again*, not what happened then.

What survives as support for the antecedent: the `source=xml` vs `source=odbc` metrics line
(**strengthened** — C4 proved the ODBC path is structurally incapable of emitting `'Nos'`, so the
fingerprint can only originate in the XML path); the 18/18 `synced_at` correlation (untested here);
the 07-27 re-scan control (untested here). The byte delta is now **removed** from this list.

Six alternative causes were tested. Five die. The survivor: `xml_parser.py:550` produces the
identical fingerprint from a healthy Tally, and is excluded only by a log line quoted inside the
doc — never by primary logs.

> **Bottom line:** You do **not** need Windows to prove that a degraded response makes our code
> write `unit='Nos', group_name=NULL`, to prove ODBC structurally cannot do it, to verify all four
> guard cases, or to build and regression-test the parser fix. You **do** need the client's Windows
> machine to fix the ODBC helper, to confirm which company is loaded, and to confirm that this
> company's Tally cannot legitimately hold a title-case `'Nos'`.

---

## 6. The v2 guard

### 6.1 What changed and why

| # | Change | Justification |
|---|---|---|
| 1 | `btrim()` on both fields | v1 matched the bare literal, so `'  Nos  '` bypassed it — demonstrated |
| 2 | `''` treated as missing | render-mode ingest writes verbatim; `group_name` lands as `''` and v1's `IS NULL` never armed |
| 3 | **Whole-row rejection** on the full degraded shape (`RETURN OLD`) | v1 protected only 2 columns; a degraded read still set `closing_qty → 0` and `part_code → NULL` |
| 4 | **`BEFORE INSERT` companion** | v1 was UPDATE-only. The RCA says a trigger "provably cannot" cover INSERT — that is true only of the `BEFORE UPDATE` trigger it describes |

Deliberately **case-sensitive** on `'Nos'`: Tally's real unit is `'NOS'` and title case is the
parser's signature. Matching case-insensitively would freeze legitimate writes.

`RETURN OLD` leaves `synced_at` unadvanced — a deliberate detection signal.

`ON CONFLICT DO UPDATE` fires `BEFORE INSERT` first, then `BEFORE UPDATE`. So on an upsert the
INSERT trigger normalises `'Nos'` → `NULL`, `EXCLUDED.unit` becomes `NULL`, and the UPDATE trigger
restores `OLD.unit`. The two compose correctly — traced deliberately, since this is the path every
sync takes.

### 6.2 THE CLIENT SQL — as applied to ztugw

> Applied to ztugw on **2026-07-28**, inside one transaction, verified immediately after.
> Idempotent: safe to re-run.

```sql
BEGIN;

CREATE OR REPLACE FUNCTION public.tb_guard_stock_unit_group()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
  v_unit  text := btrim(COALESCE(NEW.unit, ''));
  v_group text := btrim(COALESCE(NEW.group_name, ''));
BEGIN
  -- Fully degraded row (unit AND group both missing) = the 2026-07-24 fingerprint.
  -- Reject the whole row so closing_qty / rate / part_code are protected too.
  -- synced_at deliberately does not advance; that stale timestamp is the tell.
  IF (v_unit = '' OR v_unit = 'Nos') AND v_group = '' THEN
    RETURN OLD;
  END IF;

  -- Partial degradation: freeze only the affected column.
  IF v_unit = '' OR v_unit = 'Nos' THEN
    NEW.unit := OLD.unit;
  END IF;
  IF v_group = '' THEN
    NEW.group_name := OLD.group_name;
  END IF;

  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.tb_guard_stock_unit_group_ins()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  -- No OLD to restore from on INSERT, so normalise: fabricated value -> honest NULL.
  IF btrim(COALESCE(NEW.unit, '')) IN ('', 'Nos') THEN
    NEW.unit := NULL;
  END IF;
  IF btrim(COALESCE(NEW.group_name, '')) = '' THEN
    NEW.group_name := NULL;
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS tb_guard_stock_unit_group     ON public.stock_items;
DROP TRIGGER IF EXISTS tb_guard_stock_unit_group_ins ON public.stock_items;

-- The WHEN clause keeps the function un-entered on healthy syncs, so it costs
-- nothing until a bad read actually arrives.
CREATE TRIGGER tb_guard_stock_unit_group
  BEFORE UPDATE ON public.stock_items
  FOR EACH ROW
  WHEN (
    NEW.unit IS NULL OR btrim(NEW.unit) = '' OR btrim(NEW.unit) = 'Nos'
    OR NEW.group_name IS NULL OR btrim(NEW.group_name) = ''
  )
  EXECUTE FUNCTION public.tb_guard_stock_unit_group();

CREATE TRIGGER tb_guard_stock_unit_group_ins
  BEFORE INSERT ON public.stock_items
  FOR EACH ROW
  WHEN (
    NEW.unit IS NULL OR btrim(NEW.unit) = '' OR btrim(NEW.unit) = 'Nos'
    OR NEW.group_name IS NULL OR btrim(NEW.group_name) = ''
  )
  EXECUTE FUNCTION public.tb_guard_stock_unit_group_ins();

COMMIT;
```

### 6.3 Revert — instant, changes no data

```sql
DROP TRIGGER  IF EXISTS tb_guard_stock_unit_group     ON public.stock_items;
DROP TRIGGER  IF EXISTS tb_guard_stock_unit_group_ins ON public.stock_items;
DROP FUNCTION IF EXISTS public.tb_guard_stock_unit_group();
DROP FUNCTION IF EXISTS public.tb_guard_stock_unit_group_ins();
```

### 6.4 Verification query

```sql
SELECT tgname,
       CASE tgenabled WHEN 'O' THEN 'enabled' ELSE tgenabled::text END AS state,
       pg_get_triggerdef(oid) LIKE '%btrim%' AS has_btrim
FROM pg_trigger
WHERE tgrelid = 'public.stock_items'::regclass AND NOT tgisinternal
ORDER BY tgname;
```

Expect 2 rows, both `enabled`, both `has_btrim = true`. **`tgenabled` renders as `O` (letter O,
"origin") when enabled — easily misread as zero.**

### 6.5 Why the trigger must be dropped and recreated, not just replaced

`CREATE OR REPLACE FUNCTION` updates only the function body. The `WHEN (...)` clause lives on the
**trigger**, and Postgres has no way to alter it in place. Replacing only the function leaves the
old, narrower `WHEN` gate deciding whether the new logic runs at all — so `'  Nos  '` and `''`
would still never reach it. This was hit live on yynuu mid-session.

---

## 7. Safety record

### 7.1 ztugw (client production)

| Item | Before | After |
|---|---|---|
| `stock_items` rows | 17,246 | **17,246** |
| Junk rows (`btrim(unit)='Nos' AND group_name IS NULL`) | 22 | **22** |
| Triggers on `stock_items` | *none* | `tb_guard_stock_unit_group`, `tb_guard_stock_unit_group_ins` — both enabled, both `has_btrim = true` |
| `max(synced_at)` | `2026-07-25 13:16:44` | unchanged (no sync has run) |

**Pre-flight confirmed the RCA doc is stale on this point:** ztugw had **no** stock guard at apply
time — only `tb_guard_voucher_mass_delete` (a different trigger, different table, untouched). The
doc's "✅ applied to ztugw" no longer held; the earlier revert had taken.

**Writes made to ztugw:** exactly two — the DDL transaction in §6.2, and one self-cleaning smoke
test (`ZZG-QTY`, plus a scratch table `public.zz_chk`). Both deleted and verified gone. Row count
returned to 17,246 exactly. No other write of any kind was made to client production at any point
in this session.

**Smoke test result on ztugw:**

| step | stage | unit | group | qty | rate | part_code | synced_at |
|---|---|---|---|---|---|---|---|
| 1 | after seed | PAIR | TESTGRP | 5 | 100 | PC1 | 11:57:49.630646 |
| 2 | after degraded | PAIR | TESTGRP | **5** | 100 | PC1 | 11:57:49.630646 |
| 3 | after whitespace | PAIR | TESTGRP | **5** | 100 | PC1 | 11:57:49.630646 |
| 4 | after sold-out | PAIR | TESTGRP | **0** | 100 | PC1 | 11:58:19.566376 |

Steps 2–3 identical to step 1 **including `synced_at` to the microsecond** — both degraded writes
abandoned entirely, and `closing_qty` held at 5 rather than being zeroed (which is what v1 would
have allowed). Step 4 proves normal syncing is unaffected.

### 7.2 yynuu (testing)

Row count **13,253 before and after**, zero leftover test rows under any prefix. The guard trigger
was read but **never dropped, disabled, or altered** outside the deliberate v2 upgrade.

**Non-disruption suite — 10/10 passed** (`guard_v2_nondisruption.py`):

```
PASS  item sells out (qty 40 -> 0), healthy unit+group  ->  closing_qty=0
PASS  legitimate unit change PAIR -> SET                ->  unit='SET'
PASS  legitimate group change BOSCH -> ADDISON          ->  group='ADDISON'
PASS  real Tally unit 'NOS' (uppercase) writes through  ->  unit='NOS'
PASS  rate/part_code/qty churn on a healthy row         ->  rate=75 part_code='NEW' qty=9
PASS  brand-new healthy item inserts untouched          ->  unit='BOTTLE' group='PIDILITE'
PASS  bulk insert 600 healthy rows (506 ms)             ->  rows=600 nulled=0
PASS  bulk RE-SYNC of 600 rows, all qty updated (295ms) ->  rows_not_updated=0
PASS  mixed batch: 100 healthy rows applied             ->  applied=100/100
PASS  mixed batch: 100 degraded rows rejected           ->  protected=100/100
```

The **sold-out** case is the one that matters most: it is the scenario where an over-eager guard
would silently freeze stock levels and never let them reach zero. It passes because the `WHEN`
clause is false for a row carrying a real unit and a real group — the function is never entered.
The **mixed batch** is the realistic partial-degradation case: 200 rows in one payload, sorted
correctly per-row.

**One deliberate, documented behaviour change:** a sync that legitimately sends "this item now has
no stock group" is blocked (the guard restores the old group). In Tally every stock item has a
parent, so a truly null group from a healthy read is unusual. A direct `UPDATE` still works.

### 7.3 Residual untested surface — stated plainly

- **Scale.** 600 rows tested; ztugw holds 17,246. Nothing suggests a cliff — the `WHEN` clause
  short-circuits before any work — but that is a 28× extrapolation.
- **Render mode.** Untested with v2. Client is confirmed `hybrid`, so this is a fan-out concern.
- **The real engine.** All payloads were hand-built or mock-driven; no real TallyBridge sync has
  run against v2. And none can until §8 is fixed.

---

## 8. The sync outage — a second RCA

### 8.1 Timeline (BetterStack, source `2537912`)

| When | What |
|---|---|
| **07-25 13:16:49** | Last **fully successful** sync. ODBC probe ok → `stock_items` 14,461 rows via **odbc** → upload `http_status: 200`. Wrote `synced_at = 13:16:44` |
| 07-25 13:21:57, 13:27:01 | `Heartbeat found no changes - skipping sync` → clean completion. **Healthy** |
| 07-25 13:32:07 | Last event of the day (19:02 IST — end of business) |
| **07-26** | Zero client events. Sunday |
| **07-27 02:44:38** | `Starting sync` → `[Tally] Connected to TallyPrime` → **then nothing** |
| 07-27 02:44 → 03:28 | `Heartbeat: sync still running (20s interval)` × **1,687** |
| **07-27 03:28:53** | `[ERR] Sync exceeded the hard limit of 2700s` |
| 07-27 → 12:48:56 | The same cycle **12 times**, ~50 min apart. Never once completes |
| **07-28** | **Zero events, in both the hot and historical collections.** Verified twice |

> **What 07-28 does and does not tell you.** Nothing reached BetterStack today. That is *all* it
> establishes. It is equally consistent with: the machine being off, TallyBridge being closed, no
> internet on that machine, log shipping being broken, or the source key having changed. An earlier
> draft of this handout asserted "TallyBridge not running at all" — that was an inference presented
> as fact and is withdrawn. **Today is an absence of evidence, not evidence of a hang.** The hang
> below is evidenced on 07-27 only, where there are logs showing it.

> **Correction recorded deliberately.** Mid-session I read the 07-25 13:21/13:27 entries as hangs
> and concluded the outage began 07-25. That was wrong — those were healthy no-change heartbeats;
> my query filtered on `stock_items`/`Starting sync`, so the "no changes" lines were invisible and
> I read absence as failure. The outage begins **07-27 02:43**.

### 8.2 Where it hangs

Every failed attempt stops at exactly the same place:

```
[TallyBridge] Starting sync: K V ENTERPRISES
[Tally] Connected to TallyPrime
   ← 45 minutes of heartbeats, then the watchdog
```

The next step on a healthy run is `[ODBC] Probe succeeded via TallyODBC64_9000`. So the hang is
inside `odbc_bridge.probe()`, called at `sync_main.py:2105`.

### 8.3 Root cause — an unguarded blocking call inside `probe()`

**Established by evidence:** `[Tally] Connected to TallyPrime` prints only when an HTTP round-trip
to port 9000 returns product info — so **TallyPrime itself was up and answering on 07-27**. Between
that print and the next expected line (`[ODBC] Probe succeeded…`) there is nothing but variable
assignment and `odbc_bridge.probe()`. **The hang is inside `probe()`.**

**Also established:** the helper was not merely *slow*. A slow-but-alive helper makes `readline()`
block, the 30s guard fires, and `did not respond within 30s` is logged — exactly what happened on
07-24. On 07-27 that message never appears, so the simple slow case is ruled out.

**Not established — which call blocked.** `probe()` contains **three** unguarded blocking calls,
and the logs cannot distinguish them, because all three produce the identical signature: no output,
no exception, no log line.

| Candidate | Where | Blocks forever? |
|---|---|---|
| (a) `subprocess.Popen(...)` spawning PowerShell | `_start()`, ~line 246 | yes, unguarded |
| (b) `stdin.write()` + `flush()` if the child isn't draining | `_send()`, ~line 277 | yes, unguarded |
| (c) `stderr.read()` on the empty-line path | line 310 | yes, unguarded |

Candidate (c) is spelled out below because it is the least obvious of the three and the easiest to
reintroduce. **It is not asserted to be the one that fired.** The fix must bound all three.

> **Decisive test, on the client machine:** check the process list for a stray `powershell.exe`
> running `tally_odbc_helper.ps1`. Present → (b) or (c). Absent → (a).

#### Candidate (c) in detail

```python
line = read_result.get("line")
if not line:
    stderr_output = ""
    if self._proc.stderr:
        try:
            stderr_output = self._proc.stderr.read()   # ← blocks until EOF
        except Exception:
            stderr_output = ""
    raise RuntimeError(f"ODBC helper did not return a response. {stderr_output}".strip())
```

`stderr` is a `subprocess.PIPE` (`_start()`, line ~260). **`.read()` on a pipe blocks until EOF,
and EOF only arrives when the child process exits.** On this path the child has *not* exited —
`poll()` returned `None` moments earlier.

Critically, this sits **outside the 30-second guard**, which bounds only `readline()`:

```python
reader.join(timeout=ODBC_READ_TIMEOUT_SECONDS)   # 30s, line 295
if reader.is_alive():                            # only catches a SLOW read
    self.close()
    raise RuntimeError("ODBC helper did not respond within 30s ...; falling back to XML.")
```

If the PowerShell helper closes stdout but stays alive, `readline()` returns an **empty string
immediately** — the guard is satisfied — and execution falls straight into the unbounded
`stderr.read()`. The `RaiseRuntimeError` on the following line is never reached, so **nothing is
ever logged.**

This is **consistent with** the evidence — no 30s message, no error for 45 minutes, hang where
`probe()` runs, identical 12 times. So are candidates (a) and (b). Consistency is not
identification; do not read this section as a positive ID.

**A second instance of the same trap:** `close()` calls `_send({"cmd": "quit"})`, and `probe()`
calls `close()` inside its candidate loop — so the *teardown* path can wedge the same way.

### 8.4 Conclusion — both incidents are the same subsystem

| | What the ODBC helper did | Consequence |
|---|---|---|
| **07-24 11:06** | Responded slowly → 30s guard **fired** → logged → fell back to XML | Degraded XML read → `"Nos"` on 14,459 rows → **the original RCA** |
| **07-27 onward** | stdout EOF while alive → guard **did not fire** → blocked on `stderr.read()` | Sync never completes → **3 days with no data** |

The RCA's open question #13 — *why did the 07-24 XML read return unpopulated `PARENT`/`BASEUNITS`*
— now has a far better-supported frame than "Tally was mid-load or locked." **The ODBC helper on
that machine is unhealthy, and was already misbehaving on 07-24.** The XML fallback ran *because*
ODBC failed; the degraded read was the downstream consequence. On 07-24 it was slow enough to force
a bad fallback; by 07-27 it had stopped responding entirely.

This does not fully close #13 — it explains why the *XML path* was used, not why *that XML response*
came back name-only. But it relocates the investigation from "Tally is mysteriously flaky" to "one
identifiable component on one machine is failing, progressively."

**And it definitively clears the guard.** The hang is in a PowerShell subprocess on the client's
Windows machine, before any data is read and long before anything reaches Supabase. It began
02:43 UTC on 07-27, hours before the guard was applied that afternoon. A database trigger cannot
participate in a client-side subprocess hang.

### 8.5 Fix

**Immediate (unblocks the client):** restart TallyBridge on that machine — it kills the wedged
PowerShell helper. Nothing is running there today at all, so it must be started regardless.

**Code:** bound the stderr read the same way stdout is already bounded, or skip it entirely on this
path since the process is known alive:

```python
line = read_result.get("line")
if not line:
    self.close()          # tear down the wedged helper
    raise RuntimeError(
        "ODBC helper closed its output stream without responding; falling back to XML."
    )
```

Same treatment for the `stderr.read()` in the `poll() is not None` branch, and `close()` must not
be able to block.

> **Note the interaction:** this fix makes the sync fall back to **XML** — which, until the parser
> fix lands, is precisely the path that fabricates `"Nos"`. The v2 guard now on ztugw covers that.
> The two fixes are complementary, and this is a strong argument for doing the parser change soon
> rather than leaving it queued.

**Observability, unchanged from the RCA's recommendation and now more urgent:** the metrics line
(`rows / bytes / fetch_ms / source`) is insufficient to diagnose its own headline event, and
`bytes` is an untrustworthy 200-row extrapolation (§3.1). Persist the raw XML body on a degradation
predicate; replace `bytes=` with the true `len(response.content)`; log per-field populated counts
(`PARENT populated N/M`, `BASEUNITS populated N/M`) — the 07-24 sync wrote 14,459 rows with
**100% null `group_name`** and reported `status: success`.

---

## 9. Revised remediation order

1. **Restart TallyBridge on the client machine.** Nothing else can be verified until syncs resume,
   and the client has had no data for 3 days. *(New — top priority.)*
2. **Bound all three unguarded blocking calls in `odbc_bridge.probe()`** — the `Popen` spawn, the
   `stdin.write`/`flush`, and both `stderr.read()` sites — and make `close()` non-blocking. Since
   which one fired was never identified (§8.3), fixing only one is a coin flip. Small, contained,
   converts a permanent hang into the existing graceful XML fallback.
3. **Delete one of the duplicate DIVYA vouchers** — a double-post of ~₹75k is one click away.
   *(RCA §10 #1, unchanged.)*
4. **Settle the `3 Ps` vs `11` quantity question** — reproducible, larger in value than the unit
   bug, and needs the **physical challan**. Neither macOS- nor Windows-testable. *(RCA §10 #2.)*
5. **Parser fix — expanded scope.** `xml_parser.py` **351, 523, 550** + `structured_sections.json`
   **529, 931** → `None`/`null`, **not** empty string (`""` survives render-mode ingest as `''` and
   defeats the null checks). Then audit the three backend `|| "NOS"` sites. Write any degradation
   check against `""`, not `None`.
6. **Promote degradation detection (§7.2 #7).** It is the only control protecting `closing_qty` and
   `part_code`, which no trigger and no parser change addresses. Predicate validated: *if ≥N% of
   rows have blank `group_name`, treat the section as degraded and skip the master write.*
7. **Commit the guard as a real migration.** It is in no repository (§4.14–15). Dump the live DDL
   from both projects, reconcile, commit.
8. **Repair the 22 junk rows** — routine, *not* blocked by the guard (§3.2). Review before acting;
   the 19 Group-A brand rows and 3 mangled-name orphans are delete candidates, not repair
   candidates.
9. **Fan out to remaining tenants**, gated on confirming each one's `SYNC_INGEST_MODE`.
10. Record the client's TallyPrime version (`Help > About`) before any further Tally investigation.

---

## 10. Reference

### Key code sites

| Path | Role |
|---|---|
| `src/python/odbc_bridge.py` `probe()` | **the sync hang — three unguarded blocking calls (§8.3)** |
| `src/python/odbc_bridge.py:246, 277, 310` | the three candidates: `Popen` spawn · `stdin.write`/`flush` · `stderr.read()` |
| `src/python/odbc_bridge.py:295` | the 30s guard — bounds `readline()` **only** |
| `src/python/odbc_bridge.py:134` | `available()` — `os.name == "nt"`, Windows-only gate |
| `src/python/xml_parser.py:351, 523, 550` | fabricates `"Nos"` (550 is a **stock** site, not voucher) |
| `src/python/definitions/structured_sections.json:529, 931` | `"default": "Nos"` ×2 |
| `src/python/definitions/odbc_sections.json` | no default — structurally cannot emit `'Nos'` |
| `src/python/definition_extractor.py` `_is_empty` | attribute-only dict is truthy → kills the `CLOSINGBALANCE` rescue |
| `src/python/sync_main.py:1464` | tries the definition-driven path **first** |
| `src/python/sync_main.py:1980-1996` | `estimate_payload_bytes` — 200-row extrapolation |
| `src/python/sync_main.py:2105` | `odbc_bridge.probe()` — where the hang occurs |
| `src/python/tally_client.py:10` | `TALLY_URL` env var — makes the mock possible |
| `backend/src/routes/sync.ts:262-264` | voucher unit **required** — why a NULL fails closed |
| `backend/src/routes/sync.ts:2137` | render-mode verbatim upsert (no `NULLIF`) |
| `backend/src/routes/sync.ts:2629` | `seedCreatedStockItem` — third `stock_items` writer |
| `supabase/migrations/20260619_stock_items_part_code.sql` | live `tb_ingest_masters` |

### Identifiers

| | |
|---|---|
| ztugw (client prod) | `ztugwhevemibdrzqafyw` · account `rohan.psom@gmail.com` · 17,246 rows |
| ztugw company | `c47da0b1-b7dd-4589-bb66-73081022c2a3` (K V ENTERPRISES) |
| yynuu (testing) | `yynuuysvjeipawzfbeme` · account `niranjansiddharth0@gmail.com` · 13,253 rows |
| yynuu company | `74704231-5692-465f-8bea-34588dcdb86b` (K V ENTERPRISES) |
| BetterStack source | `2537912` `tallybridge-client` · table `t560530.tallybridge_client_2` |

**Row count identifies the project — check it before running anything.** 17,246 = ztugw,
13,253 = yynuu.

### BetterStack MCP

Added this session at **user scope** (`~/.claude.json`), not project scope — no `.mcp.json` in the
repo:

```
claude mcp add --transport http --scope user betterstack https://mcp.betterstack.com
```

Authenticate via `/mcp` → select → Enter. Exposes a ClickHouse `query` tool. Recent logs live in
`remote(t560530_tallybridge_client_2_logs)` (**last ~30 min only**); anything older is in
`s3Cluster(primary, t560530_tallybridge_client_2_s3)` with `_row_type = 1` for logs.

```sql
SELECT toDate(dt) AS day,
       JSONExtract(raw, 'ingest_host', 'Nullable(String)') AS host,
       count(*) AS events, min(dt) AS first_event, max(dt) AS last_event
FROM s3Cluster(primary, t560530_tallybridge_client_2_s3)
WHERE _row_type = 1 AND dt > now() - INTERVAL 14 DAY
GROUP BY day, host ORDER BY day ASC, events DESC
```

### Post-sync health check (run once syncs resume)

```sql
SELECT max(synced_at) AS last_sync_now,
       count(*) FILTER (WHERE synced_at > now() - interval '2 hours') AS updated_recently,
       count(*) FILTER (WHERE btrim(unit) = 'Nos' AND group_name IS NULL) AS junk_rows,
       count(*) AS total
FROM public.stock_items;
```

**Healthy:** `last_sync_now` past `2026-07-25 13:16:44`, `updated_recently` in the thousands,
`junk_rows` still 22, `total` ≈ 17,246.
**Roll the guard back if:** a sync demonstrably ran but `last_sync_now` did not move, or
`updated_recently` is near zero.
**Investigate, do not roll back, if:** `junk_rows` climbs above 22 — that means a degraded read got
through the INSERT path.

### Reusable artifacts

All under `/private/tmp/claude-501/-Users-siddharth-Desktop-TallyBridge/6393770b-afb8-4cc6-93c1-943b5b32a43c/scratchpad/`.
**Session-scoped — copy anything worth keeping into `src/python/tests/` before it is reaped.**

| File | Proves |
|---|---|
| `mock_tally_server.py` + `run_fetch.py` + `run_ingest.py` | **The end-to-end harness.** Fake TallyPrime gateway, stdlib only, `--mode degraded` / `--mode healthy`, drives the real unmodified engine. After the parser fix, the degraded leg must land `unit=NULL`, not `'Nos'` |
| `guard_v2_nondisruption.py` | The 10/10 normal-sync suite in §7.2. Asserts `'yynuu'` in the URL before firing; self-cleans |
| `c3_healthy_control.py` | **The regression guard for the parser fix** — 10 real units round-trip byte-exact, zero `"Nos"` |
| `c1_test_parse_stock.py`, `c1_test_definition_path.py`, `c2_test_structured_stock.py` | Both parse paths, 8 degraded shapes, ABSENT vs EMPTY vs WHITESPACE |
| `c5_test_voucher_units.py` | All three `xml_parser` sites + both JSON defaults |
| `byte_delta_test.py`, `byte_delta_test2.py`, `rows.json` | The §3.1 refutation — drives the real `estimate_payload_bytes` over 2,000 genuine rows |
| `zz07_*`, `zz08_*`, `zz09_*`, `zz10_*` | The guard case matrix, the whitespace bypass, the INSERT gap, the §3.2 refutation |

Run with `.../scratchpad/venv/bin/python` from `/Users/siddharth/Desktop/TallyBridge/src/python`.
The venv pins `xmltodict==0.13.0` to match `requirements.txt` — the dict-vs-`None` behaviour for
empty tags is version-sensitive, so this is not incidental.

---

## 11. Still open

1. **§3.3's 18/18 `synced_at` correlation was never re-verified**, and it is now the load-bearing
   evidence for the antecedent since the byte delta was removed. Needs a read against ztugw.
2. **§3.4's re-scan control** not executed. The doc's `curl` uses `push=queue`, which would write a
   **third** duplicate DIVYA voucher into client production. A `push` mode parameter does exist and
   defaults to `"none"` (`handler.py:1762,1776` — `run_purchase_pipeline(..., push_mode)` with
   `push_mode or "none"`), so a non-writing re-scan is very likely possible — **but the exact gating
   of the `push_queue` insert and the GCS write on the sale path was not traced, and must be
   confirmed by reading before any request is sent to client production.**
3. **Why *that* XML response came back name-only** (RCA §5 Q13) — reframed by §8.4 but not closed.
4. **§7.4 #12 — which company is loaded.** Three irreconcilable counts remain: 13,250 live vs
   14,459 in the sync vs 17,246 in ztugw. Until settled, live probes are not authoritative.
5. **`ARALDITE (180 G)` has a corrupt `group_name`** (`" Primary"`, ASCII 0x04). Confirmed present
   in yynuu too, so it is not a ztugw-only artifact.
6. **The guard is still in no repository.**
7. **2,787 orphan rows** — the master write never reconciles deletions. *Deferred by request.*

# Purchase Conversion — Rule Engine Status (per-vendor → fuzzy match vs Supabase stock)

Last updated: 2026-05-31.

## What this is
The conversion behind `POST /docstrange?purchase=all&source=runpod`. Pipeline:

```
Nanonets OCR (raw item text)
  → detect_vendor()                              (purchase/vendor.py)
  → build_candidate_queries(raw, vendor, code)   STAGE 1: per-vendor RULE ENGINE → canonical Tally name(s)
  → match_item_to_live_stock()                   STAGE 2: fuzzy-match canonical name vs Supabase stock_items (live, ~13k rows)
  → build_voucher_payload() → push_queue
```

Reference spec for the rules: `D:\Downloads\Vendor_Matching_Algorithms_Detailed_v4.docx`.

Key files:
- `parsing/purchase/voucher_builder.py` — `build_candidate_queries` (Stage 1), `match_item_to_live_stock` / `candidate_rows_for_item` / `similarity_score` (Stage 2), `build_voucher_payload`.
- `parsing/purchase/company_context.py` — `resolve_supabase_company_context` (fetches live stock_items + ledgers).
- `parsing/purchase_docstrange_pipeline.py` — orchestrates OCR → Stage1/2 → push.

## Benchmark (vs answer-key CSVs in `D:\Downloads\image sample`, OUT of pipeline)
Latest full run @200 DPI: **47/52 items = 90% exact** stock-name agreement.

| Vendor | items | exact |
|--------|-------|-------|
| ADDISON | 4 | 4/4 |
| CP | 6 | 6/6 |
| EMKAY (ET) | 4 | 3/4 |
| PIDILITE | 1 | 1/1 |
| RR | 24 | 22/24 |
| SAINT_GLOBAL (GNL) | 3 | 2/3 |
| TOTEM | 6 | 6/6 |
| WIKUS | 4 | 3/4 |

---

## DONE (Stage 1 rule engine implemented per vendor)

All 8 vendors have a rule branch in `build_candidate_queries`. Coverage vs v4 manual:

### ET (Emkay) — HIGH
- M2→HSS / M35→HSS-E prefix.
- Standard→TYPE: BS949/IS-II/IS-III→TAP, IS-IV→LONG TAP, D-371→TAP + `D371`.
- Modifiers: FLUTELESS/ROLL→ROLL TAP, CIR→CIR TAP.
- Tolerance: drop 6H, keep 6G/7G/7H.
- Chamfer: BOTTOMING→BOT, IS-IV BOTTOMING→TYPE C, TAPER→TPR, SP.PT→SPPT, SP.FLUTE→SPFL, O.G→O/G.
- Coating: TIN/TICN/FUTURA/GOLD kept; ROLL default TIN injection; truncated "TI"→{TIN,TICN} both tried.
- Size cleanup (strip M, strip leading 0, normalize X). Metric + imperial threads.

### ADDISON — HIGH
- M2→HSS, M35→`HSS M35` inline (not HSS-E).
- TYPE: PSTD/PARALLEL SHANK→DRILL, LONG SERIES→LONG DRILL, TAPER SHANK→T/S DRILL, END MILL→ENDMILL, HAND REAMER, M/C REAMER, CD TYPE-A→CENTRE DRILL A.
- Diameter extraction + `<name> ADDISON` suffix.

### RR — MEDIUM-HIGH
- Brand routing Miranda/Addison/YG (`rr_brand_hint`).
- YG: ISO529 #1/#2/#3→TPR/SEC/BOT, SET, GUN POINT→SPPT, SP.FLUTE→SPFL.
- YG: LONG SHANK STRAIGHT FLUTE→LONG TAP TYPE C; DIN371 TD703→ROLL TAP, others→plain TAP.
- YG carbide: JOB SS DRILL→SOLID CARBIDE DRILL, K-2 endmill/ballnose→SOLID CARBIDE [LONG] ENDMILL / BALL NOSE K2.
- Miranda/Addison: DR-JOBBER→DRILL, DR.LONG→LONG DRILL, DR-TS→T/S DRILL, END MILL, reamers, CD.

### TOTEM — MEDIUM
- TCRB kept; CST/HST→TAP, HPT→{HSS TAP, HSS-E TAP}; LH reorder; tolerance/RH/SBF2/OAL suffixes.
- CS DIE→DIE (+OD compact/spaced variants), HS DIE→HSS ROUND DIE; drills, centre drills, BS-number.

### GNL — MEDIUM (most master-list-heavy)
- Family routing by code prefix: DX→AG-N QUICK CUT, FA→FLAP WHEEL/MOP WHEEL, FP/AB→FLAP DISK/EMERY BELT, V→GRINDING WHEEL, PX→POLISHING/UNIFIED, VG2/3/4→EMERY PASTE, NES3→EMERY CLOTH, S1→COMBINATION STONE, R340/R305→EMERY CLOTH ROLLS, foam tape.

### CP — via CP_EXACT_ITEM_QUERIES map (catalog_rules.py)
### PIDILITE — STEELGRIP TAPE 3/4" [colour]
### STANLEY (Lenox) — BIMETAL BANDSAW from CP/CL/LXP series
### WIKUS — BIMETAL BANDSAW from ECOFLEX/PRIMAR/NOVOFLEX series

---

## NOT DONE / KNOWN GAPS (work on later)

### 1. Raw-text fallback can out-score the rule-engine query  ★ highest impact
`build_candidate_queries` appends a raw passthrough query (`f"{text} ET"` etc.) as a safety net. In Stage 2 the **raw query sometimes wins** the fuzzy match and lands on the WRONG stock item at score 100 (e.g. EMKAY `M14×1.5 bottoming` matched a `5/16" UNF roll tap`). This is the main cause of the EMKAY/RR misses.
- Fix idea: prefer/weight the deterministic canonical query; penalize or drop the raw passthrough; or only fall back to raw when no canonical query scores above threshold.

### 2. Master-list-dependent rules NOT derivable from text (v4 §"Master-list dependencies")
These need a lookup table the rule engine does not yet have:
- **ET**: default coating injection for FLUTELESS/ROLL when PDF omits coating; "TI"→TIN vs TICN disambiguation; verbatim spacing quirks.
- **TOTEM**: HPT→HSS vs HSS-E routing (keyed off Forbes product code); CS DIE default-OD drop/keep table; SEC chamfer sometimes-dropped rule.
- **GNL**: ~70% is master-list — dimension orientation (W×L vs L×W) per item, bore include/exclude, sub-brand keep/drop (SpitFire, ALKON PREMIUM vs ALKON PRE), grade-string rewrite (PA46/54→AA46/64), specific code→name (COMBINATION STONE LIST NO 109).
- **RR/Miranda**: decimal-form lookup (1.20 vs 1.2 vs 2.0→2) is provably NOT text-derivable; BS-series centre-drill size keep/drop; imperial→metric-in-parens scope.
- **ADDISON**: trailing-zero keep/strip split (M2 19.0 kept vs M35 always stripped); BS-number imperial-size drop; hand-tap ISO coarse-pitch expansion (M10→1.5) — partial.

### 3. Vendor-specific gaps to verify
- ET imperial DIN variants (NPTF/BSP) — extra DIN query added but unverified.
- TOTEM HPT HSS-vs-HSS-E both tried (relies on fuzzy to pick) — not deterministic.
- GNL grinding-wheel grade strings — only loose grade regex; rewrites not handled.
- CP relies on a hand-maintained exact-query map, not general rules.

### 4. OCR-upstream issues (not rule-engine bugs)
- RR garbled descriptions (e.g. `K-2 CARBIDE 4FL EX-LONG E/M5X5X25X75`) → low score, correctly flagged weak.
- WIKUS occasional leading-digit misread on invoice number.

---

## How to extend (later)
1. Fix gap #1 first (cheap, biggest accuracy gain) — change query ranking in `match_item_to_live_stock` so canonical > raw.
2. Add the master-list lookup tables (gap #2) — ideally sourced from the existing Supabase `purchase_matching_api` (321 rows) which already maps invoice-desc→tally-name for known items; the exact-map shortcut (`match_item_via_purchase_matching`) already uses it when an exact hit exists.
3. Per-vendor, port remaining v4 rules into the matching branches.

Confidence ladder from v4: ET ✅ HIGH, ADDISON ✅ HIGH, RR ⚠️ MED-HIGH, TOTEM/GNL ⚠️ MEDIUM, CP/PIDILITE/STANLEY/WIKUS = newer/1-sample.

# Session Handout — ET Description Repair Fixes

**File modified:** `parsing/purchase_image_pipeline.py`  
**Branch:** `tally-bridge-tallyprime-reorder_level_final_attempt`  
**Invoice tested against:** GN25Y-34486 (Emkay Tools Limited)  
**Ground truth:** `ET5Final.csv` — final voucher_items rows from Supabase/Tally  

---

## Background

The purchase OCR pipeline reads PDF invoices via PaddleOCR, repairs the raw text per vendor, builds candidate search queries, then fuzzy-matches against stock item names in Supabase (synced from TallyPrime). For ET (Emkay Tools), all stock names follow a structured pattern:

```
HSS-E TAP <size> <modifiers> <coating> ET
```

Three bugs were found by running `challan_sale_1778495739427.pdf` and comparing matched stock names against the CSV ground truth.

---

## Bug 1 — TIN coating dropped when OCR truncates to single char `"T"`

### Where
`purchase_image_pipeline.py` line 790 (ET branch of `build_candidate_queries`)

### What happened
The invoice description column has a fixed width. PaddleOCR reads only what fits, truncating the coating suffix:
- Full text: `F IS-III M35 M12 X 1.25 6H SP.FLUTE (E Series) TIN`
- OCR output: `F IS-III M35 M12 X 1.25 6H SP.FLUTE (E Series) T` ← only 1 char of TIN

The coating detection guard was:
```python
if text.endswith(" TI") or " TI " in f" {text} ":
    coating_candidates.extend(["TIN", "TICN"])
```

With only `"T"` at the end, `endswith(" TI")` is `False` → no coating detected → query built as `"HSS-E TAP 12 X 1.25 SPFL ET"` (no TIN) → matched non-TIN stock item.

### Fix
```python
# Before
if text.endswith(" TI") or " TI " in f" {text} ":

# After
if text.endswith(" TI") or text.endswith(" T") or " TI " in f" {text} ":
```

Adding `text.endswith(" T")` catches the single-char truncation and adds TIN/TICN to coating candidates.

### Items fixed
- Item 1: `HSS-E TAP 12 X 1.25 SPFL ET` → **`HSS-E TAP 12 X 1.25 SPFL TIN ET`**
- Item 5: Already matched correctly (description ended in `"T"` but score happened to be right — now formally correct)

---

## Bug 2 — Bare fallback query overwrites TIN match via strict `>` comparison

### Where
`purchase_image_pipeline.py` line 810 (ET branch of `build_candidate_queries`)

### What happened
For item 3 (M16 X 1.5), the description ended in `"TI"` → coating correctly detected as `["TIN", "TICN"]` → TIN query built: `"HSS-E TAP 16 X 1.5 SPFL TIN ET"`. But line 810 **unconditionally** appended a bare fallback:

```python
queries.append(normalize_space(f"{brand} TAP {size_part} {suffix_core} ET"))
# → "HSS-E TAP 16 X 1.5 SPFL ET"  (no coating)
```

In `match_item_to_live_stock`, Supabase returns stock rows alphabetically. `"HSS-E TAP 16 X 1.5 SPFL ET"` sorts before `"HSS-E TAP 16 X 1.5 SPFL TIN ET"`. When the bare fallback query is run against the non-TIN candidate it scores 100 (exact match). The TIN candidate also scores 100, but the comparison uses strict `>`:

```python
if score > best_score:   # 100 > 100 → False → TIN never wins
```

So the first 100-scoring candidate (non-TIN, alphabetically earlier) locked in and could never be displaced.

### Fix
```python
# Before
queries.append(normalize_space(f"{brand} TAP {size_part} {suffix_core} ET"))

# After
if not coating_candidates:
    queries.append(normalize_space(f"{brand} TAP {size_part} {suffix_core} ET"))
```

The bare fallback is now only added when no coating was detected — matching the same conditional pattern already used for the primary query at line 801–802. When coatings are present, only coating-qualified queries are emitted and the non-TIN variant cannot score 100 against them.

### Items fixed
- Item 3: `HSS-E TAP 16 X 1.5 SPFL ET` → **`HSS-E TAP 16 X 1.5 SPFL TIN ET`**

---

## Bug 3 — OCR merges `M35` grade token with adjacent text, breaking size extraction

### Where
`purchase_image_pipeline.py` inside `repair_et_description` (line ~537)

### What happened
PaddleOCR drops spaces between tokens when they share the same bounding box or are too close horizontally. Two cases observed:

**Case A — `M35` merged with imperial size:**
- Printed: `F BS 949 M35 7/16" - 20 UNF FLUTELESS O.G.`
- OCR output: `FBS 949 M357/16-20 UNFFLUTELESS`
- `M35` (HSS-E steel grade) + `7/16` (imperial thread size) collapsed to `M357/16`
- `extract_imperial_thread` regex looks for `\d+/\d+` patterns — `357/16` doesn't parse as `7/16`
- → No valid imperial thread found → query wrong → matched `HSS-E ROLL TAP 10-24 UNC TIN ET` (score 69)

**Case B — `M35` merged with metric size, also preceded by merged prefix:**
- Printed: `F D-371 M35 M6 X 0.75 6G SP.PT. TIN`
- OCR output: `FD-371M35M6 X 0.75 6G SP.PT.TIN`
- `F` + `D-371` + `M35` + `M6` all collapsed into `FD-371M35M6`
- `extract_metric_size_pitch` regex requires `\bM` (word boundary before M) to find metric size
- In `FD-371M35M6`: `1` and `M` are both word chars → no `\b` between them → `M6` not detected as metric size
- → `size_part` empty → query built without size → matched wrong D371 variant (score 84)

### Root cause
`M35` in ET items is always the **HSS-E steel grade indicator** — never part of the size. It should always be a standalone token separated by spaces from neighbouring text.

### Fix
Two regex rules added at the end of `repair_et_description`, before `normalize_space`:

```python
# Rule 1: add space BEFORE M35 when directly preceded by alphanumeric
# Fixes: FD-371M35M6 → FD-371 M35M6
cleaned = re.sub(r"(?<=[A-Z0-9])M35", " M35", cleaned)

# Rule 2: add space AFTER M35 when directly followed by letter or digit 1-9
# Fixes: M35M6 → M35 M6  |  M357/16 → M35 7/16
# Excludes digit 0 to avoid false split on M350 (valid metric thread)
cleaned = re.sub(r"M35(?=[A-Z1-9])", "M35 ", cleaned)
```

After both rules + `normalize_space`:
- `FD-371M35M6` → `FD-371 M35 M6` → `extract_metric_size_pitch` finds `M6 X 0.75` ✓
- `M357/16` → `M35 7/16` → `extract_imperial_thread` finds `7/16" UNF` ✓

### Items fixed
- Item 4: `HSS-E ROLL TAP 10-24 UNC TIN ET` (score 69) → **`HSS-E ROLL TAP 7/16" UNF O/G TIN ET`** (score 100)
- Item 8: `HSS-E TAP D371 8 X .75 6G SPPT TIN ET` (score 84) → **`HSS-E TAP D371 6 X .75 6G SPPT TIN ET`** (score 100)

---

## Before vs After — Full Table

Ground truth: `ET5Final.csv`

| # | Raw OCR Description | Before Fix | After Fix | Correct (CSV) |
|---|---|---|---|---|
| 1 | FIS-III M35 M12 X 1.25 6H SP.FLUTE (E SERIES) **T** | HSS-E TAP 12 X 1.25 SPFL ET ❌ | HSS-E TAP 12 X 1.25 SPFL TIN ET ✅ | HSS-E TAP 12 X 1.25 SPFL TIN ET |
| 2 | FIS-III M35 M12 X1.5 6H SP.PTE SERIESTIN | HSS-E TAP 12 X 1.5 SPPT TIN ET ✅ | HSS-E TAP 12 X 1.5 SPPT TIN ET ✅ | HSS-E TAP 12 X 1.5 SPPT TIN ET |
| 3 | FIS-III M35 M16 X 1.5 6H SP.FLUTE (E SERIES) **TI** | HSS-E TAP 16 X 1.5 SPFL ET ❌ | HSS-E TAP 16 X 1.5 SPFL TIN ET ✅ | HSS-E TAP 16 X 1.5 SPFL TIN ET |
| 4 | FBS 949 **M357/16**-20 UNFFLUTELESS O.G.E SERI | HSS-E ROLL TAP 10-24 UNC TIN ET ❌ | HSS-E ROLL TAP 7/16" UNF O/G TIN ET ✅ | HSS-E ROLL TAP 7/16" UNF O/G TIN ET |
| 5 | F BS 949 M35 5/16-24 UNF SP.FLUTE (E SERIES) T | HSS-E TAP 5/16" UNF SPFL TIN ET ✅ | HSS-E TAP 5/16" UNF SPFL TIN ET ✅ | HSS-E TAP 5/16" UNF SPFL TIN ET |
| 6 | F BS 949 M35 1/4"-20UNC SP.FLUTE E SERIES)**TI** | HSS-E TAP 1/4" UNC SPFL TIN ET ✅ | HSS-E TAP 1/4" UNC SPFL TIN ET ✅ | HSS-E TAP 1/4" UNC SPFL TIN ET |
| 7 | FIS-II M35 M4 X 0.7 6H SP.PT(E SERIESFUTURA | HSS-E TAP 4 X .7 SPPT FUTURA ET ✅ | HSS-E TAP 4 X .7 SPPT FUTURA ET ✅ | HSS-E TAP 4 X .7 SPPT FUTURA ET |
| 8 | **FD-371M35M6** X 0.75 6G SP.PTTIN | HSS-E TAP D371 10 X 1.25 6G SPPT TIN ET ❌ | HSS-E TAP D371 6 X .75 6G SPPT TIN ET ✅ | HSS-E TAP D371 6 X .75 6G SPPT TIN ET |

**Names: 4/8 → 7/8**

---

## Remaining Known Issue

**Item 7 — quantity = 0**  
OCR reads `10` as `L0` (character `L` misread as digit `1`). `parse_numeric_token` returns `0`.  
The amount (₹4,012.20) and rate (₹401.22) are correct — only the quantity field is wrong in the extracted row.  
This is a PaddleOCR character recognition issue, not a phrase logic issue. Fix would require either a post-processing numeric sanity check (`if qty == 0 and amount > 0 and rate > 0: qty = round(amount / rate)`) or a higher DPI render pass.

---

## Summary of Code Changes

```python
# purchase_image_pipeline.py

# --- Bug 1 & 2: ET coating detection (line ~790, ~810) ---

# Line 790 — catch single-char TIN truncation
- if text.endswith(" TI") or " TI " in f" {text} ":
+ if text.endswith(" TI") or text.endswith(" T") or " TI " in f" {text} ":

# Line 810 — bare fallback only when no coating detected
- queries.append(normalize_space(f"{brand} TAP {size_part} {suffix_core} ET"))
+ if not coating_candidates:
+     queries.append(normalize_space(f"{brand} TAP {size_part} {suffix_core} ET"))

# --- Bug 3: M35 token separation (inside repair_et_description, line ~537) ---
+ cleaned = re.sub(r"(?<=[A-Z0-9])M35", " M35", cleaned)
+ cleaned = re.sub(r"M35(?=[A-Z1-9])", "M35 ", cleaned)
```

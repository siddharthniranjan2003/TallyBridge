# Session Handout — Purchase OCR Pipeline: Full Context

**Branch:** `tally-bridge-tallyprime-reorder_level_final_attempt`  
**Primary files modified:** `parsing/purchase_image_pipeline.py`, `parsing/purchase_paddle_runner.py`  
**Test invoice:** `challan_sale_1778495739427.pdf` (Emkay Tools Limited, GN25Y-34486)  
**Ground truth:** `ET5Final.csv` — final voucher_items rows confirmed in Supabase/TallyPrime  

---

## Architecture Overview

Three files collaborate to process purchase invoice PDFs:

### `parsing/n8n_minicpm_server.py`
- Flask server that listens on port 5003
- Receives PDF via HTTP POST `/?type=purchase&company=<name>`
- Calls `purchase_paddle_runner.py` as a subprocess, captures JSON output
- Returns structured pipeline response to n8n or direct caller

### `parsing/purchase_paddle_runner.py`
- Entry point: receives PDF path + company name
- Renders PDF pages to images (via `pdf2image` / `pymupdf`)
- **OpenCV preprocessing** (new) — deskew, denoise, sharpen before OCR
- Runs PaddleOCR in dual-pass: raw pass on preprocessed image, highres pass on original
- Detects table rows and columns (bounding box based)
- Parses numeric columns: quantity, rate, amount
- Calls `purchase_image_pipeline.py` for all business logic

### `parsing/purchase_image_pipeline.py`
- Vendor detection (from party name)
- Per-vendor description repair (normalise OCR artifacts)
- Query building — per-vendor phrase logic → candidate stock name strings
- Fuzzy match against `stock_items` table in Supabase (synced from TallyPrime)
- Voucher assembly: ledger entries, item rows, discount scaling
- Returns full voucher JSON ready to push to TallyPrime

---

## ET (Emkay Tools) Phrase Logic

All ET stock names follow this structure:
```
HSS-E TAP <size> <modifiers> <coating> ET
```

`build_candidate_queries` (ET branch) extracts:
- **Brand:** `HSS-E` or `HSS`
- **Family:** `TAP`, `ROLL TAP`, `LONG TAP`, `CIR TAP`
- **Size:** metric (`M12 X 1.25`) or imperial (`7/16" UNF`)
- **Coating candidates:** `TIN`, `TICN`, `FUTURA`, `GOLD`
- **Suffix tokens:** `6G`, `7G`, `6H`, `SPFL`, `SPPT`, `BOT`, `TPR`, `O/G`, `D371`

---

## Bugs Fixed

### Bug 1 — TIN coating dropped when OCR truncates to single char `"T"`

**File:** `purchase_image_pipeline.py` ~line 790  

OCR reads only what fits in fixed-width description column. "TIN" at end becomes just "T".

```python
# Before
if text.endswith(" TI") or " TI " in f" {text} ":

# After
if text.endswith(" TI") or text.endswith(" T") or " TI " in f" {text} ":
    coating_candidates.extend(["TIN", "TICN"])
```

**Items fixed:** Item 1 (`M12 X 1.25 SPFL`), Item 3 (`M16 X 1.5 SPFL`)

---

### Bug 2 — Bare fallback query overwrites TIN match (strict `>` comparison)

**File:** `purchase_image_pipeline.py` ~line 810  

A bare (no-coating) fallback query was unconditionally appended even when TIN/TICN was detected. Supabase returns rows alphabetically — the non-TIN stock name sorts before the TIN one. Both scored 100; strict `>` blocked the TIN match from ever winning.

```python
# Before
queries.append(normalize_space(f"{brand} TAP {size_part} {suffix_core} ET"))

# After
if not coating_candidates:
    queries.append(normalize_space(f"{brand} TAP {size_part} {suffix_core} ET"))
```

**Item fixed:** Item 3 (`M16 X 1.5 SPFL`)

---

### Bug 3 — OCR merges `M35` grade token with adjacent text

**File:** `purchase_image_pipeline.py` inside `repair_et_description` ~line 537  

`M35` = HSS-E steel grade. Always a standalone token, but OCR collapses spaces:
- `M357/16` — grade fused with imperial size → `extract_imperial_thread` can't parse `357/16`
- `FD-371M35M6` — prefix + grade + metric size merged → `\bM` regex fails (no word boundary between `1` and `M`)

`\b` word boundary can't split fused alphanumeric runs, so two explicit rules were added:

```python
# Rule 1: space BEFORE M35 when directly preceded by alphanumeric
cleaned = re.sub(r"(?<=[A-Z0-9])M35", " M35", cleaned)

# Rule 2: space AFTER M35 when followed by letter or digit 1-9
# Excludes 0 to avoid false split on M350 (valid metric thread)
cleaned = re.sub(r"M35(?=[A-Z1-9])", "M35 ", cleaned)
```

Both rules added before `return normalize_space(cleaned)` in `repair_et_description`.

**Items fixed:** Item 4 (`M357/16` → `7/16" UNF O/G TIN`), Item 8 (`FD-371M35M6` → `D371 6 X .75`)

---

## OpenCV Preprocessing (new — `purchase_paddle_runner.py`)

### Environment variables (added after `PADDLE_HIGHRES_DET_LIMIT`)

```python
PADDLE_PREPROCESS        = (os.getenv("MINICPM_PADDLE_PREPROCESS",   "1") or "1").strip() not in {"0","false","False"}
PADDLE_PREPROCESS_DESKEW = (os.getenv("MINICPM_PADDLE_DESKEW",       "1") or "1").strip() not in {"0","false","False"}
PADDLE_PREPROCESS_DENOISE= (os.getenv("MINICPM_PADDLE_DENOISE",      "1") or "1").strip() not in {"0","false","False"}
PADDLE_PREPROCESS_SHARPEN= (os.getenv("MINICPM_PADDLE_SHARPEN",      "1") or "1").strip() not in {"0","false","False"}
PADDLE_DESKEW_MAX_ANGLE  = float(os.getenv("MINICPM_PADDLE_DESKEW_MAX_ANGLE", "10") or "10")
```

### `_deskew(gray)` function

```python
def _deskew(gray: np.ndarray) -> np.ndarray:
    thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    coords = np.column_stack(np.where(thresh > 0))
    if len(coords) < 100:
        return gray
    angle = cv2.minAreaRect(coords)[-1]
    if angle < -45:
        angle = 90.0 + angle
    if abs(angle) < 0.3 or abs(angle) > PADDLE_DESKEW_MAX_ANGLE:
        return gray
    h, w = gray.shape
    center = (w // 2, h // 2)
    M = cv2.getRotationMatrix2D(center, angle, 1.0)
    return cv2.warpAffine(gray, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
```

### `preprocess_for_ocr(image)` function

```python
def preprocess_for_ocr(image: np.ndarray) -> np.ndarray:
    if not PADDLE_PREPROCESS:
        return image
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if PADDLE_PREPROCESS_DESKEW:
        gray = _deskew(gray)
    if PADDLE_PREPROCESS_DENOISE:
        gray = cv2.fastNlMeansDenoising(gray, h=4, templateWindowSize=7, searchWindowSize=21)
    if PADDLE_PREPROCESS_SHARPEN:
        blur = cv2.GaussianBlur(gray, (0, 0), sigmaX=1)
        gray = cv2.addWeighted(gray, 1.3, blur, -0.3, 0)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
```

**Key tuning note:** `h=4` for denoise. `h=10` caused regressions — it softened thin digit strokes, dropping qty to 0 for items 4, 7, 8. Lower is safer for crisp printed invoices.

### Wiring in `build_purchase_ocr_payload`

```python
source_image, source_meta = load_source_image(source_path)
processed_image = preprocess_for_ocr(source_image)
tokens = extract_ocr_tokens(processed_image, mode="raw")      # preprocessed
# ...
high_tokens = extract_ocr_tokens(source_image, mode="highres") # original — not preprocessed
```

Highres pass intentionally uses the original image to avoid double-processing on the already-magnified region.

### What deskew fixed

Deskew corrected the invoice_total OCR split from `"10, 520.21"` → `"10,520.21"` which fixed:
- `invoice_total`: `70,773.41` → `68,966.00` (correct)
- 3% discount now correctly applied via `adjust_items_to_target_subtotal` (scale factor ~0.97)
- All 8 item amounts adjusted downward by ~3%

---

## Numeric Sanitization (new — `purchase_paddle_runner.py`)

### `parse_numeric_token` — added character substitutions

```python
# Added L, I, B to existing O, S substitutions
text = text.replace("O", "0").replace("S", "5").replace("L", "1").replace("I", "1").replace("B", "8")
```

Fixes `L0` → `10` (qty misread), `I` → `1`, `B` → `8`.

### `sanitize_numeric_row(quantity, rate, amount)` — new function

Cross-validates `qty × rate ≈ amount`. If they don't agree within 2%:
1. If `qty == 0` and `rate > 0`: infer qty = `round(amount / rate)`, accept if within 2%
2. If `qty > 0`: check if rate has a lost decimal point — try `rate/10`, `rate/100`, `rate/1000`, `rate*10`

```python
def sanitize_numeric_row(quantity: float, rate: float, amount: float) -> tuple[float, float, float]:
    if amount <= 0:
        return quantity, rate, amount
    if quantity <= 0 and rate > 0:
        inferred = round(amount / rate)
        if inferred > 0 and abs(inferred * rate - amount) / amount < 0.02:
            quantity = inferred
    if quantity > 0 and rate > 0:
        expected = amount / quantity
        if abs(rate - expected) / max(expected, 1e-9) > 0.02:
            for divisor in (10.0, 100.0, 1000.0, 0.1):
                candidate = rate / divisor
                if candidate > 0 and abs(candidate - expected) / max(expected, 1e-9) < 0.02:
                    rate = round(candidate, 2)
                    break
    return quantity, rate, amount
```

Wired in `parse_table_rows` immediately after qty/rate/amount extraction:
```python
quantity, rate, amount = sanitize_numeric_row(quantity, rate, amount)
```

**Fixes:** Item 6 rate `47736` → `477.36` (OCR dropped decimal), Item 7 qty `0` → `10` (inferred from amount÷rate).

---

## Before vs After — Full Results Table

Ground truth: `ET5Final.csv`

| # | Raw OCR Description | Pipeline Stock Match | Score | Issues |
|---|---|---|---|---|
| 1 | FIS-III M35 M12 X 1.25 6H SP.FLUTE (E SERIES) **T** | **HSS-E TAP 12 X 1.25 SPFL TIN ET** ✅ | 100 | Was missing TIN (Bug 1) |
| 2 | FIS-III M35 M12 X1.5 6H SP.PT E SERIES TIN | HSS-E TAP 12 X 1.5 SPPT TIN ET ✅ | 100 | — |
| 3 | FIS-III M35 M16 X 1.5 6H SP.FLUTE (E SERIES) **TI** | **HSS-E TAP 16 X 1.5 SPFL TIN ET** ✅ | 100 | Was picking non-TIN (Bugs 1+2) |
| 4 | FBS 949 **M357/16**-20 UNFFLUTELESS O.G. E SERI | **HSS-E ROLL TAP 7/16" UNF O/G TIN ET** ✅ | 100 | `M357/16` merge (Bug 3) |
| 5 | F BS 949 M35 5/16-24 UNF SP.FLUTE (E SERIES) T | HSS-E TAP 5/16" UNF SPFL TIN ET ✅ | 100 | — |
| 6 | F BS 949 M35 1/4"-20UNC SP.FLUTE E SERIES) TI | HSS-E TAP 1/4" UNC SPFL TIN ET ✅ | 100 | Rate `47736`→`477.36` (sanitize) |
| 7 | FIS-II M35 M4 X 0.7 6H SP.PT (E SERIES) FUTURA | HSS-E TAP 4 X .7 SPPT FUTURA ET ✅ | 100 | Qty `L0`→`10` (L→1 subst.) |
| 8 | **FD-371M35M6** X 0.75 6G SP.PT TIN | **HSS-E TAP D371 6 X .75 6G SPPT TIN ET** ✅ | 100 | `FD-371M35M6` merge (Bug 3) |

**Stock names: 4/8 → 8/8 ✅ | Quantities: 7/8 → 8/8 ✅ | Rates: 7/8 → 8/8 ✅**

---

## Remaining Known Issue

**Item 7 qty originally `L0` via OCR** — fixed via `L→1` substitution in `parse_numeric_token`. The underlying root cause is PaddleOCR misreading `1` as `L` in low-contrast printed fonts. The `sanitize_numeric_row` fallback (`qty=0` → infer from amount÷rate) is a secondary safety net if the substitution ever fails.

---

## Push JSON Format

The pipeline response includes `push_queue_payload` key ready to POST to TallyPrime:

**Endpoint:** `POST http://localhost:3001/push-voucher` (backend proxy) or `http://localhost:3002/push-voucher` (direct Python bridge)

```json
{
  "company_name": "K V ENTERPRISES",
  "voucher_payload": {
    "party_name": "EMKAY TOOLS LIMITED",
    "date": "2026-03-31",
    "voucher_type": "Purchase",
    "voucher_number": "GN25Y-34486",
    "reference": "GN25Y-34486",
    "narration": "Purchase invoice GN25Y-34486",
    "inventory_ledger_name": "PURCHASE GST",
    "ledger_entries": [
      { "ledger_name": "EMKAY TOOLS LIMITED", "amount": 68966.00, "is_deemed_positive": false },
      { "ledger_name": "PURCHASE GST",        "amount": 58445.79, "is_deemed_positive": true  },
      { "ledger_name": "IGST",                "amount": 10520.21, "is_deemed_positive": true  }
    ],
    "items": [
      { "stock_item_name": "HSS-E TAP 12 X 1.25 SPFL TIN ET",       "quantity": 10, "unit": "NOS", "rate": 1227.42, "amount": 12274.20, "godown_name": "Main Location" },
      { "stock_item_name": "HSS-E TAP 12 X 1.5 SPPT TIN ET",        "quantity":  5, "unit": "NOS", "rate": 1016.28, "amount":  5081.40, "godown_name": "Main Location" },
      { "stock_item_name": "HSS-E TAP 16 X 1.5 SPFL TIN ET",        "quantity": 10, "unit": "NOS", "rate": 1566.00, "amount": 15660.00, "godown_name": "Main Location" },
      { "stock_item_name": "HSS-E ROLL TAP 7/16\" UNF O/G TIN ET",  "quantity":  5, "unit": "NOS", "rate": 1336.50, "amount":  6682.50, "godown_name": "Main Location" },
      { "stock_item_name": "HSS-E TAP 5/16\" UNF SPFL TIN ET",      "quantity": 15, "unit": "NOS", "rate":  536.76, "amount":  8051.40, "godown_name": "Main Location" },
      { "stock_item_name": "HSS-E TAP 1/4\" UNC SPFL TIN ET",       "quantity": 10, "unit": "NOS", "rate":  477.36, "amount":  4773.60, "godown_name": "Main Location" },
      { "stock_item_name": "HSS-E TAP 4 X .7 SPPT FUTURA ET",       "quantity": 10, "unit": "NOS", "rate":  401.22, "amount":  4012.20, "godown_name": "Main Location" },
      { "stock_item_name": "HSS-E TAP D371 6 X .75 6G SPPT TIN ET", "quantity":  5, "unit": "NOS", "rate":  743.58, "amount":  3717.90, "godown_name": "Main Location" }
    ]
  }
}
```

### Validation rules (`tally_pusher.py`)

| Field | Rule |
|---|---|
| `quantity` | Must be `> 0` — throws `ValueError`, blocks push |
| `is_deemed_positive` | `false` for party/creditor, `true` for purchase + tax ledgers |
| `date` | `YYYY-MM-DD` or `YYYYMMDD` |
| `voucher_type` | Must be one of `"Purchase"`, `"Sales"`, `"GST SALE"`, `"GST PURCHASE"` |
| Ledger balance | Party amount = sum of purchase + tax ledger amounts |

### Discount / scaling

`adjust_items_to_target_subtotal` scales all item amounts:
```
scale = target_subtotal / raw_item_total
target_subtotal = invoice_total - tax_total
```
Getting `invoice_total` right from OCR is critical — a bad read kills the entire discount application.

---

## Files Changed Summary

| File | What changed |
|---|---|
| `parsing/purchase_image_pipeline.py` | Bug 1: `endswith(" T")` catch; Bug 2: `if not coating_candidates` guard; Bug 3: two `re.sub` M35 rules in `repair_et_description` |
| `parsing/purchase_paddle_runner.py` | 5 env var constants; `_deskew()` function; `preprocess_for_ocr()` function; wiring in `build_purchase_ocr_payload`; `L/I/B` substitutions in `parse_numeric_token`; `sanitize_numeric_row()` function + wiring in `parse_table_rows` |
| `parsing/session_handout_m35rule.md` | Detailed per-bug documentation for M35 / TIN bugs (subset of this file) |

---

## Quick Reference: Running a Test

```powershell
# Start server
py -3 D:\Desktop\TallyBridge\parsing\n8n_minicpm_server.py --serve

# Send a PDF
curl -X POST "http://127.0.0.1:5003?type=purchase&company=K%20V%20ENTERPRISES" `
  -F "file=@D:\Downloads\challan_sale_1778495739427.pdf"
```

The response `matched_items` array is the per-item OCR result.  
The `push_queue_payload` key is the TallyPrime-ready JSON.

---

## Scan Quality Context

Three PDFs were compared for OCR quality:

| PDF | Type | Quality | Notes |
|---|---|---|---|
| `ET5.pdf` (16.7 KB) | Native/digital | Excellent | `pdfplumber` text extraction would bypass OCR entirely |
| `challan_sale_1778495739427.pdf` | Scanned | Good | Consistent layout, clean columns, OCR reliable |
| `emkay_new.pdf` | Scanned (photocopied?) | Poor | Low contrast, skew, merged bounding boxes |

For best results with physical paper scans: scan at ≥300 DPI, use consistent lighting, avoid document creases.

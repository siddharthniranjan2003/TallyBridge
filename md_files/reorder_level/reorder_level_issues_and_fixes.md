# Reorder Level — Issues, Explanations & Recommended Fixes

File audited: `backend/src/routes/sync.ts`
Date: 2026-04-13

---

## Issue 1 — Dead code block inside the `/reorder-levels` route handler

**Location:** `sync.ts` lines 2028–2131 (inside `router.get("/reorder-levels", ...)`)

**What it is:**
The old quantity-based purchase implementation (reading from the `purchases` table, 90-day window, no scenario classification) was never deleted. It lives after:

```ts
return res.json(report);   // line 2026 — control exits here
```

Because `return res.json(report)` fires unconditionally, everything below it — including the entire legacy block — is unreachable code. TypeScript does not error on this because the block is syntactically valid.

**Why it matters:**
- Adds ~100 lines of noise that maintainers may mistake for live logic.
- References the `purchases` table and hardcoded date `"2019-03-31"` — both obsolete — which can confuse future debugging.
- If the `return` is accidentally removed or refactored, the old block silently re-activates with wrong behavior (qty-based, no threshold, no scenario).

**Recommended fix:**
Delete lines 2028–2131 entirely. The `try/catch` closing brace on line 2130 must remain; the dead content between the `return` and that brace is what should go.

```ts
// BEFORE (keep only the lines shown, delete the rest)
router.get("/reorder-levels", requireApiKey, async (req, res) => {
  try {
    const companyLookup = await resolveCompanyLookup({ ... });
    if (companyLookup.status !== 200) {
      return res.status(companyLookup.status).json({ error: companyLookup.error });
    }

    const report = await buildInventoryIntelligenceReport({ ... });
    return res.json(report);

    // ← DELETE everything from here to the end of the try block
  } catch (err: any) {
    console.error("[ReorderLevels] Error:", err.message);
    res.status(500).json({ error: err.message });
  }
});
```

---

## Issue 2 — `voucher_type` match uses wrong casing for GST Sale

**Location:** `sync.ts` line 804

**What it is:**

```ts
.eq("voucher_type", "GST SALE")
```

The spec (and the Tally TDL that populates the table) stores the type as `"GST Sale"` (mixed case). The query does an exact-match `.eq()` which is case-sensitive in Postgres by default.

**Why it matters:**
If any vouchers were synced with `"GST Sale"` (or `"Gst Sale"`) instead of `"GST SALE"`, they are silently excluded from the 6-month sale aggregation. `avg_sale_6m` will be 0 for all items, every item will fall into the `col_i === 0` branch of the classifier, and the scenario distribution will be completely wrong.

**Recommended fix — option A (preferred):** Use `.ilike()` for a case-insensitive exact match:

```ts
.ilike("voucher_type", "gst sale")
```

**Recommended fix — option B:** Verify at the DB level what exact string is stored and update the constant to match, then add a comment explaining the exact casing:

```ts
// voucher_type is stored exactly as "GST Sale" by TallyBridge TDL v2+
.eq("voucher_type", "GST Sale")
```

Either way, confirm the actual value with:

```sql
SELECT DISTINCT voucher_type FROM vouchers WHERE voucher_type ILIKE '%sale%' LIMIT 20;
```

---

## Issue 3 — `classifyInventoryScenario` returns `null` when all three columns are non-zero (unclassified items)

**Location:** `sync.ts` lines 754–758

**What it is:**

```ts
if (lastMonthPurchasePaise === 0) {
  return closingStockPaise <= avgSale6mPaise ? "I" : "H";
}

return null;   // reached when col_i > 0, col_ii > 0, col_iii > 0
```

The classification tree handles:
- `col_i === 0` → A/B/C/D
- `col_iii === 0` → E/F/G
- `col_ii === 0` → H/I

But when **all three columns are non-zero simultaneously** (`avgSale6m > 0`, `lastMonthPurchase > 0`, `closingStock > 0`), the function returns `null`. The caller increments `unclassifiedCount` and skips the item entirely — it never appears in the report.

**Why it matters:**
This is the most common real-world case: an item has ongoing sales, was restocked last month, and has stock on hand. These are the most important items to manage and they are currently invisible in the report.

**Spec mapping from `KV_Inventory_Intelligence_FINAL.md`:**
- Scenario H (`DRAIN`): `col_i > 0`, `col_iii > 0`, `col_iii > col_i` (stock exceeds avg sale — draining slowly)
- Scenario I (`STARVE`): `col_i > 0`, `col_iii > 0`, `col_iii <= col_i` (stock at or below avg sale — risk of stock-out)

The purchase column (`col_ii`) value when all three are non-zero is not part of the H/I decision — only `col_i` vs `col_iii` matters.

**Recommended fix:**

Replace the final branch of `classifyInventoryScenario`:

```ts
// CURRENT (wrong — only checks col_ii === 0)
if (lastMonthPurchasePaise === 0) {
  return closingStockPaise <= avgSale6mPaise ? "I" : "H";
}

return null;

// FIXED — H/I apply whenever col_i > 0 AND col_iii > 0, regardless of col_ii
// The col_ii===0 check above already handled the pure case;
// the same H/I logic applies when col_ii is also non-zero.
return closingStockPaise <= avgSale6mPaise ? "I" : "H";
```

Full corrected function:

```ts
function classifyInventoryScenario(
  avgSale6mPaise: number,
  lastMonthPurchasePaise: number,
  closingStockPaise: number,
): InventoryScenarioCode | null {
  if (avgSale6mPaise === 0) {
    if (lastMonthPurchasePaise === 0) {
      return closingStockPaise > 0 ? "A" : null;   // all-zero = not a real item
    }
    if (lastMonthPurchasePaise === closingStockPaise) return "B";
    return closingStockPaise < lastMonthPurchasePaise ? "C" : "D";
  }

  if (closingStockPaise === 0) {
    if (lastMonthPurchasePaise === avgSale6mPaise) return "E";
    return lastMonthPurchasePaise < avgSale6mPaise ? "F" : "G";
  }

  // col_i > 0 AND col_iii > 0 → H or I (col_ii value does not affect this decision)
  return closingStockPaise <= avgSale6mPaise ? "I" : "H";
}
```

This removes the `null` return for all valid data combinations. The only remaining `null` case is when all three paise values are 0 (a stock item with zero activity and zero closing stock — correctly excluded).

---

## Issue 4 — `reorder_level` formula uses `* 2` but spec says `÷ 3`

**Location:** `sync.ts` line 887

**What it is:**

```ts
const reorderLevel = avgSale6m * 2;
```

The `reorder_level_code_changes_summary.md` documents this as `avg_sale_6m * 2`.
The original spec in `KV_Inventory_Intelligence_FINAL.md` and `reorder_levels_implementation_plan.md` states:

> Reorder Level = total_6m_sale ÷ 3

Since `avg_sale_6m = total_6m_sale / 6`, the formula in terms of `avg_sale_6m` is:

```
reorder_level = total_6m_sale / 3
              = (avg_sale_6m * 6) / 3
              = avg_sale_6m * 2
```

**Assessment:** The `* 2` formulation is mathematically correct and equivalent to `total_6m_sale ÷ 3`. This is **not a bug**, but the comment in the code should make this equivalence explicit to avoid confusion in future reviews.

**Recommended fix (documentation only):**

```ts
// reorder_level = total_6m_sale / 3 = (avg_sale_6m * 6) / 3 = avg_sale_6m * 2
const reorderLevel = avgSale6m * 2;
```

---

## Issue 5 — `reorder_at` is a redundant alias for `reorder_level`

**Location:** `sync.ts` lines 888 and 923

**What it is:**

```ts
const reorderAt = reorderLevel;   // always identical
```

Both `reorder_level` and `reorder_at` are emitted in the response JSON with the same value. This doubles the field count without adding information.

**Why it matters:**
Clients (n8n, Telegram, frontend) reading this response get two identically-valued fields. If `reorder_at` was intended to be a different concept (e.g. a safety-stock adjusted trigger), it was never implemented as such.

**Recommended fix:**
Remove `reorder_at` from the `InventoryIntelligenceItem` type and the response object, and have clients use `reorder_level` exclusively. If `reorder_at` is intentionally a separate concept, add a comment explaining how it differs and implement the distinction.

---

## Issue 6 — `needs_reorder` uses closing stock value vs. reorder level, but scenario I is already the stock-out signal

**Location:** `sync.ts` line 927

**What it is:**

```ts
needs_reorder: closingStockPaise <= toPaise(reorderAt),
```

This flags an item as needing reorder whenever `closing_stock_value ≤ reorder_level`. Because `reorder_level = avg_sale_6m * 2`, this will flag every scenario-I item (where `closingStock ≤ avgSale6m`) **and** some scenario-H items (where `avgSale6m < closingStock ≤ reorderLevel`).

This is technically correct behavior, but there is an inconsistency: scenario-A items (zero sales, zero purchases, only closing stock) will **never** have `needs_reorder = true` because their `reorder_level` is 0, so `closingStockPaise <= 0` is false for any positive stock. This is probably the right behavior but worth documenting.

**Recommended fix (documentation only):**

```ts
// needs_reorder: closing stock is at or below the reorder trigger point
// Note: scenario-A items always have reorder_level=0 so needs_reorder=false by design
needs_reorder: closingStockPaise <= toPaise(reorderAt),
```

---

## Summary Table

| # | Severity | Issue | Fix Type |
|---|----------|-------|----------|
| 1 | Medium | Dead code block (100 lines, old qty-based logic) | Delete lines 2028–2131 |
| 2 | High | `voucher_type` exact-match may miss real vouchers due to casing | Use `.ilike()` or verify DB casing |
| 3 | Critical | Items with all 3 columns non-zero are unclassified and excluded | Remove `null` return, fall through to H/I |
| 4 | None (doc) | `* 2` formula looks wrong but is mathematically correct | Add clarifying comment |
| 5 | Low | `reorder_at` duplicates `reorder_level` in response | Remove `reorder_at` or define its distinction |
| 6 | None (doc) | `needs_reorder` edge case for scenario-A items | Add comment |

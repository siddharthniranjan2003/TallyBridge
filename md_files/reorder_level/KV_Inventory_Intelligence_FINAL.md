# Inventory Intelligence System — K.V. Enterprises
### Final Specification (All Corrections Applied)

---

## 1. Nature of a Hardware Trader

A hardware trader like K.V. Enterprises **buys from distributors/manufacturers and resells** to contractors, construction sites, and retailers. They deal in pipes, fittings, wires, fasteners, tools etc.

Key characteristics:
- **No manufacturing** — pure buy-resell, so stock management is everything
- Demand is **project-driven and seasonal** — bulk orders followed by silence
- Hundreds of SKUs with wildly different movement patterns
- **Cash is locked in stock** — dead inventory = cash you can't use
- Suppliers give credit, customers demand credit — timing of purchase vs sale is critical
- A missed reorder on a high-demand item = **customer walks to competitor**

---

## 2. The Three Columns

| Column | What It Measures |
|--------|-----------------|
| **I** | **Average monthly sales over last 6 months** — (total 6M sales ÷ 6). Smooths out spikes, shows *real repeatable demand* |
| **II** | **Last 1 month purchase value** — the trader's *most recent restocking decision* |
| **III** | **Closing stock value today** — *capital currently locked* in this item |

---

## 3. Relationship Between Column I and Column II

The gap between these two is the **core intelligence** of the system:

| Avg Monthly Sale (I) | Last 1M Purchase (II) | What It Means |
|---------------------|----------------------|---------------|
| High | High | Healthy — buying matches demand |
| High | Low or NIL | **Danger** — demand exists, not restocking |
| NIL | High | **Risk** — buying with no demand signal |
| NIL | NIL | Completely dead item |
| Equal | Equal | Efficient, well-managed item |

The **6-month average** eliminates noise — one big contractor order in month 2 doesn't fake a trend. If avg is consistently ₹1000/month, that's proven demand.

The **1-month purchase** is the trader's latest decision. The question the system asks: *did that decision match the demand signal?*

---

## 4. Scenario Table — Fully Corrected

| Scenario | Avg Monthly Sale (I) | Last 1M Purchase (II) | Closing Stock (III) | What It Means |
|----------|---------------------|----------------------|--------------------|---------------|
| **A** | NIL | NIL | 2000/- | **Non-moving / Dead stock** — no sales, no buying, just sitting |
| **B** | NIL | 2000 | 2000/- | **Sales about to begin** — trader stocked up anticipating upcoming demand, sales haven't started yet |
| **C** | NIL | 2000 | 1000/- or NIL | **Stock went negative** — purchase was done to cover a negative stock position *(see below)* |
| **D** | NIL | 2000 | 3000/- | **Dead stock still being purchased** — item was already not moving, trader kept buying anyway, making it worse |
| **E** | 1000 | 1000 | NIL | **Fastest moving inventory** — selling and buying simultaneously, stock gets absorbed instantly, zero buffer |
| **F** | 2000 | 1000 or NIL | NIL | **Item intentionally being reduced** — high sales consumed all stock, trader not repurchasing, consciously exiting this SKU |
| **G** | 2000 | 3000 | NIL | **High demand item** — selling heavily, purchasing even more to keep up, yet stock still hits NIL — can't keep it on shelves |
| **H** | 1000 | NIL | 3000 | **Item intentionally being reduced** — mid wind-down, still has stock to liquidate, deliberately stopped reordering |
| **I** | 1000 | NIL | 1000 or 500 | **Very high demand, under-purchased** — proven sustained demand over 6 months, but trader bought nothing recently, nearly out of stock *(see below)* |

---

## 5. F vs H — Both "Reducing" but Different Stages

| | F | H |
|--|---|---|
| Avg Monthly Sale | Higher (2000) | Lower (1000) |
| Last 1M Purchase | NIL or minimal | NIL |
| Closing Stock | **NIL** — fully wound down | **3000** — still liquidating |
| Stage | **End state** — item effectively discontinued | **In progress** — still clearing shelf |

---

## 6. Special Explanations

### How Can Stock Go Negative? (Scenario C)
In Tally, negative stock appears when:
- A **sale entry is made before the purchase bill is entered** — goods received and sold same day, purchase pending
- **Delivery challan issued** before supplier invoice arrives
- **Branch transfers** not recorded in time

The closing stock being NIL or very low despite a recent purchase means that purchase was used to **fill the negative gap** — the restock corrected the bookkeeping shortfall, not a real surplus.

### How Can Scenario I Be "Very High Demand, Under-Purchased"?
- 6-month avg sale = ₹1000/month → **proven, consistent, real demand**
- Last 1M purchase = NIL → trader bought **nothing**
- Closing stock = only ₹500–1000 → **about to stock out**

Possible reasons the trader didn't reorder:
- Supplier out of stock / supply chain gap
- Trader misjudged timing of reorder
- Cash flow constraint
- Simple oversight on a busy item

This is the **highest priority alert** — real buyers exist, history proves it, but stockout is imminent. Every day without stock is a lost sale and a customer potentially lost to a competitor permanently.

---

## 7. Color Coding & Priority Ranking

| Scenario | Condition | Color | Priority |
|----------|-----------|-------|----------|
| **I** | Very high demand & under-purchased | 🟢 Green | **1** — Act immediately |
| **C** | Stock went negative / Proxy stock | 🔴 Red | **2** |
| **D** | Dead stock still being purchased | 🔴 Red | **3** |
| **A** | Non-moving / Dead stock | 🟠 Orange | **4** |
| **G** | High demand item | 🟢 Green | **4** |
| **B** | Sales about to begin | 🟢 Green | **5** |
| **F** | Item intentionally being reduced (end state) | 🟠 Orange | **6** |
| **E** | Fastest moving inventory | 🟢 Green | **7** |
| **H** | Item intentionally being reduced (mid wind-down) | 🟠 Orange | **8** |

---

## 8. Threshold Filter

> If **any one** of Column I, II, or III exceeds **₹5 lakhs** for an item → include it in the report

This ensures:
- A slow seller with **₹5L+ of dead stock** appears (capital risk)
- A fast mover with **₹5L+ avg monthly sales** appears (revenue opportunity)
- A recent **₹5L+ purchase** with no sales appears (bad buying decision flagged)

Items below ₹5L in all three columns are filtered out to keep the report focused.

---

## 9. Reorder Level Formula

### What Is Reorder Level?
> **When an item's closing stock value drops TO the Reorder Level → trigger a purchase order immediately.**

It is the minimum stock threshold. Falling at or below it means: *place an order now or risk stockout before the next supply arrives.*

### Formula Derivation

```
Reorder Level = Average Monthly Sale × 2
             = (Total 6-month sales ÷ 6) × 2
             = Total 6-month sales ÷ 3
```

This simplification means only **one number is needed from Tally** — total sales of that item over last 6 months. No monthly bucketing required.

### Example
- Item: GI Pipe 1 inch
- Total sales last 6 months = ₹60,000
- Avg monthly sale = ₹60,000 ÷ 6 = **₹10,000/month**
- Reorder Level = ₹10,000 × 2 = **₹20,000**
- → When closing stock drops to ₹20,000 → **trigger purchase order**

### Why 2 Months as the Buffer?
- Suppliers have MOQs — can't order just 17 days worth
- Credit cycles run 30–45 days — purchase timing must align
- Project-driven demand spikes — a contractor can buy 3 months of avg stock in one day
- Supply uncertainty — hardware items from NCR suppliers aren't always immediately available
- Cash flow planning — 2 months gives visibility to plan finances around the purchase

### Ordering Quantity
> **Order Qty = Reorder Level − Current Closing Stock** (if positive, i.e. already below reorder level)

Example:
- Reorder Level = ₹20,000
- Current closing stock = ₹8,000
- **Order needed = ₹12,000 worth** — top up back to the 2-month buffer

### Reorder Level by Scenario

| Scenario | Closing Stock vs Reorder Level | Action |
|----------|-------------------------------|--------|
| **E** | Way below — already NIL | **Urgent reorder** |
| **G** | Way below — already NIL, high volume | **Urgent reorder** |
| **I** | At or below — nearly out | **Immediate reorder** |
| **F** | Below but intentional | No reorder — trader chose to exit |
| **H** | Above — still liquidating | No action needed yet |
| **A** | Reorder level = 0 (no sales) | **Never reorder** — clear existing stock first |
| **B** | Reorder level = 0 technically (sales not started) | **Watch** — reorder kicks in once sales begin |
| **D** | Reorder level = 0 but purchases still happening | **Stop purchasing** — alert required |

---

## 10. Telegram Trigger Words

| Scenario | What It Means | Trigger Word | Why |
|----------|--------------|--------------|-----|
| **A** | Non-moving / Dead stock | `INERT` | Completely still, no life |
| **B** | Sales about to begin | `ONSET` | Start is imminent |
| **C** | Stock went negative | `GHOST` | Stock exists on paper but not in reality |
| **D** | Dead stock still being purchased | `BLOAT` | Getting fatter with unwanted stock |
| **E** | Fastest moving inventory | `BLAZE` | On fire, burns through instantly |
| **F** | Intentionally reducing — end state | `TAPER` | Consciously winding down |
| **G** | High demand, can't keep on shelf | `SURGE` | Demand overwhelming supply |
| **H** | Intentionally reducing — mid wind-down | `DRAIN` | Slowly emptying out |
| **I** | Very high demand, under-purchased | `STARVE` | Hungry market, empty hands |

### Example Telegram Alert Format
```
🟢 STARVE — MS Angle 40x40
Stock: ₹500 | Avg Sale: ₹1000/mo | Order needed

🔴 GHOST — GI Pipe 1"
Stock: NIL | Purchase: ₹2000 | Sale: NIL

🔴 BLOAT — PVC Elbow 2"
Stock: ₹3000 | Purchase: ₹2000 | Sale: NIL

🟢 BLAZE — GI Wire 8 Gauge
Stock: NIL | Purchase: ₹1000 | Avg Sale: ₹1000/mo

🟠 DRAIN — MS Flat 25x5
Stock: ₹3000 | Purchase: NIL | Avg Sale: ₹1000/mo
```

---

## 11. Reorder Level — Data Schema

**Lead time buffer:** 7 days base + 10 days added = ~17 days total

**Sales average basis:** Last 6 months (≈ 180 days)

**Two outputs per item:**
- **Ordering Quantity** — how much to order (Reorder Level − Closing Stock)
- **Ordering Frequency** — how often to order

**Purchase side fields:** Item#, Qty, Supplier, Rate

**Sale side fields:** Item#, Qty, Customer, Rate, Sl#

**Stated goal:** → *Maximize sales & minimize stock*

---

## 12. Full Report Output Per Item

| Field | Value |
|-------|-------|
| Scenario | A / B / C ... I |
| Trigger Word | INERT / ONSET / GHOST etc. |
| Color | 🔴 / 🟠 / 🟢 |
| Priority | 1–8 |
| Avg Monthly Sale (6M) | ₹ value |
| Last 1M Purchase | ₹ value |
| Closing Stock | ₹ value |
| Reorder Level | Total 6M sales ÷ 3 |
| Reorder Triggered? | Yes — closing stock ≤ reorder level / No |
| Order Quantity Needed | Reorder Level − Closing Stock (if positive) |

---

*This is a per-SKU health dashboard built on top of TallyPrime data, designed to surface the right action for each item — reorder, exit, investigate, or watch.*

# Inventory Intelligence System — K.V. Enterprises

## Nature of a Hardware Trader

A hardware trader like K.V. Enterprises **buys from distributors/manufacturers and resells** to contractors, construction sites, and retailers. They deal in pipes, fittings, wires, fasteners, tools etc.

Key characteristics:
- **No manufacturing** — pure buy-resell, so stock management is everything
- Demand is **project-driven and seasonal** — bulk orders followed by silence
- Hundreds of SKUs with wildly different movement patterns
- **Cash is locked in stock** — dead inventory = cash you can't use
- Suppliers give credit, customers demand credit — timing of purchase vs sale is critical
- A missed reorder on a high-demand item = **customer walks to competitor**

---

## The Three Columns

| Column | What It Measures |
|--------|-----------------|
| **I** | Average monthly sales over last 6 months (total ÷ 6) — smooths out spikes, shows *real repeatable demand* |
| **II** | Last 1 month purchase value — the trader's *most recent restocking decision* |
| **III** | Closing stock value today — *capital currently locked* in this item |

---

## Relationship Between Column I and Column II

This gap is the **core intelligence** of the system:

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

## Scenario Table — Fully Corrected

| Scenario | Avg Monthly Sale (I) | Last 1M Purchase (II) | Closing Stock (III) | What It Means |
|----------|---------------------|----------------------|--------------------|---------------|
| **A** | NIL | NIL | 2000/- | **Non-moving / Dead stock** — no sales, no buying, just sitting |
| **B** | NIL | 2000 | 2000/- | **Sales about to begin** — trader stocked up anticipating upcoming demand, sales haven't started yet |
| **C** | NIL | 2000 | 1000/- or NIL | **Stock went negative** — purchase was done to cover a negative stock position *(see below)* |
| **D** | NIL | 2000 | 3000/- | **Dead stock still being purchased** — item was already not moving, trader kept buying anyway, making it worse |
| **E** | 1000 | 1000 | NIL | **Fastest moving inventory** — selling and buying simultaneously, stock gets absorbed instantly, zero buffer |
| **F** | 2000 | 1000 or NIL | NIL | **Item intentionally being reduced** — high sales consumed all stock, trader is not repurchasing, consciously exiting this SKU |
| **G** | 2000 | 3000 | NIL | **High demand item** — selling heavily, purchasing even more to keep up, yet stock still hits NIL — can't keep it on shelves |
| **H** | 1000 | NIL | 3000 | **Item intentionally being reduced** — mid wind-down, still has stock to liquidate, deliberately stopped reordering |
| **I** | 1000 | NIL | 1000 or 500 | **Very high demand, under-purchased** — proven sustained demand over 6 months, but trader bought nothing recently, nearly out of stock *(see below)* |

---

## F vs H — Both "Reducing" but Different Stages

| | F | H |
|--|---|---|
| Sales | Higher (2000) | Lower (1000) |
| Purchase | NIL or minimal | NIL |
| Closing Stock | **NIL** — fully wound down | **3000** — still liquidating |
| Stage | **End state** — item effectively discontinued | **In progress** — still clearing shelf |

---

## How Can Stock Go Negative? (Scenario C)

In Tally, negative stock appears when:
- A **sale entry is made before the purchase bill is entered** — goods received and sold same day, purchase pending
- **Delivery challan issued** before supplier invoice arrives
- **Branch transfers** not recorded in time

The closing stock being NIL or very low despite a recent purchase means that purchase was used to **fill the negative gap** — the restock corrected the bookkeeping shortfall, not a real surplus.

---

## How Can Scenario I Be "Very High Demand, Under-Purchased"?

- 6-month avg sale = 1000/month → **proven, consistent, real demand**
- Last 1M purchase = NIL → trader bought **nothing**
- Closing stock = only 500–1000 → **about to stock out**

Possible reasons the trader didn't reorder:
- Supplier out of stock / supply chain gap
- Trader misjudged timing of reorder
- Cash flow constraint — couldn't afford it
- Simple oversight on a busy item

This is the **highest priority alert** in the whole system — real buyers exist, history proves it, but stockout is imminent. Every day without stock is a lost sale and a customer potentially lost permanently to a competitor.

---

## Color Coding & Priority Ranking

| Condition | Color | Priority |
|-----------|-------|----------|
| Very high demand & under-purchased (Scenario I) | 🟢 Green | **1** — Act immediately |
| Stock went negative / Proxy stock (Scenario C) | 🔴 Red | **2** |
| Dead stock still being purchased (Scenario D) | 🔴 Red | **3** |
| Non-moving / Dead stock (Scenario A) | 🟠 Orange | **4** |
| High demand item (Scenario G) | 🟢 Green | **4** |
| Sales about to begin (Scenarios B, H) | 🟢 Green | **5** |
| Items we're reducing trading of (Scenarios F, H) | 🟠 Orange | **6** |
| Fastest moving inventory (Scenario E) | 🟢 Green | **7** |
| Items reducing — lower urgency variant | 🟠 Orange | **8** |

---

## Threshold Filter

> If **any one** of Column I, II, or III exceeds **₹5 lakhs** for an item → include it in the report

This ensures:
- A slow seller with **₹5L+ of dead stock** appears (capital risk)
- A fast mover with **₹5L+ avg monthly sales** appears (revenue opportunity)
- A recent **₹5L+ purchase** with no sales appears (bad buying decision flagged)

Small-value items below ₹5L in all three columns are filtered out to keep the report focused.

---

## Reorder Level Logic

**Lead time buffer:** 7 days base + 10 days added = ~17 days total

**Sales average basis:** Last 6 months (≈ 180 days), with recency weighting across 120-day / shorter windows

**Reorder Level formula:** `2 × avg consumption ÷ 3` (adjusted for lead time)

**Two outputs per item:**
- **Ordering Quantity** — how much to order
- **Ordering Frequency** — how often to order

**Stated goal:** → *Maximize sales & minimize stock*

---

## Data Schema for Reorder Calculation

**Purchase side per item:** Item#, Qty, Supplier, Rate

**Sale side per item:** Item#, Qty, Customer, Rate, Sl#

---

*This is a per-SKU health dashboard built on top of Tally data, designed to surface the right action for each item — reorder, exit, investigate, or watch.*

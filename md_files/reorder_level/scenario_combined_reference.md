# Scenario + Trigger Word + Formula — Combined Reference Table

> All values are **amount/value based**, not quantity based.
> - **I** = avg_sale_6m (average monthly sale over last 6 months)
> - **II** = last_month_purchase
> - **III** = closing_stock_value

| Scenario | Avg Monthly Sale (I) | Last 1M Purchase (II) | Closing Stock (III) | What It Means | Trigger Word | Why | Identification Formula |
|----------|---------------------|----------------------|--------------------|---------------|--------------|-----|----------------------|
| **A** | NIL | NIL | 2000/- | Non-moving / Dead stock — no sales, no buying, just sitting | `INERT` | Completely still, no life | `I = 0 AND II = 0 AND III > 0` |
| **B** | NIL | 2000 | 2000/- | Sales about to begin — trader stocked up anticipating upcoming demand, sales haven't started yet | `ONSET` | Start is imminent | `I = 0 AND II = III` |
| **C** | NIL | 2000 | 1000/- or NIL | Stock went negative — purchase was done to cover a negative stock position | `GHOST` | Stock exists on paper but not in reality | `I = 0 AND III < II` |
| **D** | NIL | 2000 | 3000/- | Dead stock still being purchased — item already not moving, trader kept buying anyway | `BLOAT` | Getting fatter with unwanted stock | `I = 0 AND III > II` |
| **E** | 1000 | 1000 | NIL | Fastest moving inventory — selling and buying simultaneously, zero buffer | `BLAZE` | On fire, burns through instantly | `I > 0 AND III = 0 AND II = I` |
| **F** | 2000 | 1000 or NIL | NIL | Item intentionally being reduced — end state, consciously exiting this SKU | `TAPER` | Consciously winding down | `I > 0 AND III = 0 AND II < I` |
| **G** | 2000 | 3000 | NIL | High demand item — can't keep it on shelves despite heavy purchasing | `SURGE` | Demand overwhelming supply | `I > 0 AND III = 0 AND II > I` |
| **H** | 1000 | NIL | 3000 | Item intentionally being reduced — mid wind-down, still liquidating | `DRAIN` | Slowly emptying out | `I > 0 AND II = 0 AND III > I` |
| **I** | 1000 | NIL | 1000 or 500 | Very high demand, under-purchased — nearly out of stock despite proven demand | `STARVE` | Hungry market, empty hands | `I > 0 AND II = 0 AND III <= I` |

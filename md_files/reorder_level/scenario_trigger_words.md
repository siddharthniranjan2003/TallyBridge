# Scenario Trigger Words — K.V. Enterprises Telegram Alerts

## Trigger Word Table

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

---

## Example Telegram Alert Format

```
🔴 GHOST — GI Pipe 1"
Stock: NIL | Purchase: ₹2000 | Sale: NIL

🟢 STARVE — MS Angle 40x40
Stock: ₹500 | Avg Sale: ₹1000/mo | Order needed

🟠 BLOAT — PVC Elbow 2"
Stock: ₹3000 | Purchase: ₹2000 | Sale: NIL

🟢 BLAZE — GI Wire 8 Gauge
Stock: NIL | Purchase: ₹1000 | Avg Sale: ₹1000/mo

🟠 DRAIN — MS Flat 25x5
Stock: ₹3000 | Purchase: NIL | Avg Sale: ₹1000/mo
```

---

## Color by Scenario

| Trigger Word | Color | Priority |
|-------------|-------|----------|
| `STARVE` | 🟢 Green | 1 — Act immediately |
| `GHOST` | 🔴 Red | 2 |
| `BLOAT` | 🔴 Red | 3 |
| `INERT` | 🟠 Orange | 4 |
| `SURGE` | 🟢 Green | 4 |
| `ONSET` | 🟢 Green | 5 |
| `TAPER` | 🟠 Orange | 6 |
| `BLAZE` | 🟢 Green | 7 |
| `DRAIN` | 🟠 Orange | 8 |

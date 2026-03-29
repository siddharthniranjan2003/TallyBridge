import { useEffect, useState } from "react";
import { get, fmt } from "../api/client";

export default function Inventory() {
  const [items, setItems] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    get("/api/sync/stock")
      .then(r => setItems(r.stock_items || []))
      .finally(() => setLoading(false));
  }, []);

  const totalValue = items.reduce((s, i) => s + Math.abs(i.closing_value || 0), 0);

  return (
    <div style={{ padding: 32 }}>
      <h1 style={{ fontSize: 20, fontWeight: 600, marginBottom: 6 }}>Inventory</h1>
      <p style={{ fontSize: 13, color: "#aaa", marginBottom: 20 }}>
        {items.length} items · Total value: {fmt(totalValue)}
      </p>

      {loading ? <p style={{ color: "#aaa" }}>Loading...</p> : (
        <div style={{ background: "#fff", borderRadius: 12, border: "1px solid #f0f0f0" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ color: "#bbb", fontSize: 11, textTransform: "uppercase" }}>
                {["Item Name", "Unit", "Closing Qty", "Rate", "Value", "Status"].map(h => (
                  <th key={h} style={{
                    padding: "10px 20px",
                    textAlign: ["Closing Qty", "Rate", "Value"].includes(h) ? "right" : "left",
                    fontWeight: 500, borderBottom: "1px solid #f5f5f5"
                  }}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {items.map((item, i) => (
                <tr key={i} style={{ borderBottom: "1px solid #fafafa" }}>
                  <td style={{ padding: "11px 20px", fontWeight: 500 }}>{item.name}</td>
                  <td style={{ padding: "11px 20px", color: "#888" }}>{item.unit || "Nos"}</td>
                  <td style={{ padding: "11px 20px", textAlign: "right", fontWeight: 500 }}>
                    {item.closing_qty || 0}
                  </td>
                  <td style={{ padding: "11px 20px", textAlign: "right", color: "#888" }}>
                    {item.rate ? fmt(item.rate) : "—"}
                  </td>
                  <td style={{ padding: "11px 20px", textAlign: "right", fontWeight: 500 }}>
                    {fmt(item.closing_value)}
                  </td>
                  <td style={{ padding: "11px 20px" }}>
                    <span style={{
                      fontSize: 11, padding: "2px 8px", borderRadius: 20, fontWeight: 500,
                      background: (item.closing_qty || 0) > 0 ? "#dcfce7" : "#fee2e2",
                      color: (item.closing_qty || 0) > 0 ? "#166534" : "#dc2626",
                    }}>
                      {(item.closing_qty || 0) > 0 ? "In stock" : "Out of stock"}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
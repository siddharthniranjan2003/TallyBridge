import { useEffect, useState } from "react";
import { get, fmt } from "../api/client";

export default function Sales() {
  const [vouchers, setVouchers] = useState<any[]>([]);
  const [filter, setFilter] = useState("All");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    get("/api/sync/vouchers")
      .then(r => setVouchers(r.vouchers || []))
      .finally(() => setLoading(false));
  }, []);

  const types = ["All", "Sales", "Purchase", "Receipt", "Payment"];
  const filtered = filter === "All" ? vouchers : vouchers.filter(v => v.voucher_type === filter);
  const total = filtered.reduce((s, v) => s + Math.abs(v.amount || 0), 0);

  return (
    <div style={{ padding: 32 }}>
      <h1 style={{ fontSize: 20, fontWeight: 600, marginBottom: 6 }}>Vouchers</h1>

      <div style={{ display: "flex", gap: 8, marginBottom: 20, flexWrap: "wrap" }}>
        {types.map(t => (
          <button key={t} onClick={() => setFilter(t)} style={{
            padding: "6px 14px", borderRadius: 20, fontSize: 12, cursor: "pointer",
            border: filter === t ? "none" : "1px solid #e5e7eb",
            background: filter === t ? "#1a1a2e" : "#fff",
            color: filter === t ? "#fff" : "#666", fontWeight: 500,
          }}>{t}</button>
        ))}
        <span style={{ marginLeft: "auto", fontSize: 13, color: "#888", alignSelf: "center" }}>
          {filtered.length} entries · Total: {fmt(total)}
        </span>
      </div>

      {loading ? <p style={{ color: "#aaa" }}>Loading...</p> : (
        <div style={{ background: "#fff", borderRadius: 12, border: "1px solid #f0f0f0" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ color: "#bbb", fontSize: 11, textTransform: "uppercase" }}>
                {["Date", "Type", "Voucher #", "Party", "Items", "Amount"].map(h => (
                  <th key={h} style={{
                    padding: "10px 20px",
                    textAlign: h === "Amount" ? "right" : "left",
                    fontWeight: 500, borderBottom: "1px solid #f5f5f5"
                  }}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {filtered.map((v, i) => (
                <tr key={i} style={{ borderBottom: "1px solid #fafafa" }}>
                  <td style={{ padding: "10px 20px", color: "#888" }}>{v.date}</td>
                  <td style={{ padding: "10px 20px" }}>
                    <span style={{
                      fontSize: 11, padding: "2px 8px", borderRadius: 20,
                      background: v.voucher_type === "Sales" ? "#dcfce7" :
                        v.voucher_type === "Purchase" ? "#fef3c7" : "#f3f4f6",
                      color: v.voucher_type === "Sales" ? "#166534" :
                        v.voucher_type === "Purchase" ? "#92400e" : "#374151",
                      fontWeight: 500
                    }}>
                      {v.voucher_type}
                    </span>
                  </td>
                  <td style={{ padding: "10px 20px", color: "#888" }}>{v.voucher_number || "—"}</td>
                  <td style={{ padding: "10px 20px" }}>{v.party_name || "—"}</td>
                  <td style={{ padding: "10px 20px", color: "#888" }}>
                    {(v.voucher_items || []).length} items
                  </td>
                  <td style={{ padding: "10px 20px", textAlign: "right", fontWeight: 500 }}>
                    {fmt(v.amount)}
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
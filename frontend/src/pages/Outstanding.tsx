import { useEffect, useState } from "react";
import { get, fmt } from "../api/client";

export default function Outstanding() {
  const [data, setData] = useState<any[]>([]);
  const [tab, setTab] = useState<"receivable" | "payable">("receivable");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    get("/api/sync/outstanding")
      .then(r => setData(r.outstanding || []))
      .finally(() => setLoading(false));
  }, []);

  const filtered = data.filter(o => o.type === tab);
  const total = filtered.reduce((s, o) => s + Math.abs(o.pending_amount || 0), 0);

  const agingColor = (days: number) =>
    days > 90 ? "#dc2626" : days > 60 ? "#d97706" : days > 30 ? "#ca8a04" : "#16a34a";

  return (
    <div style={{ padding: 32 }}>
      <h1 style={{ fontSize: 20, fontWeight: 600, marginBottom: 6 }}>Outstanding</h1>

      {/* Tabs */}
      <div style={{ display: "flex", gap: 8, marginBottom: 20 }}>
        {(["receivable", "payable"] as const).map(t => (
          <button key={t} onClick={() => setTab(t)} style={{
            padding: "7px 18px", borderRadius: 20, fontSize: 13,
            border: tab === t ? "none" : "1px solid #e5e7eb",
            background: tab === t ? "#1a1a2e" : "#fff",
            color: tab === t ? "#fff" : "#666", cursor: "pointer", fontWeight: 500,
          }}>
            {t === "receivable" ? "To Receive" : "To Pay"}
          </button>
        ))}
        <div style={{
          marginLeft: "auto", padding: "7px 18px", borderRadius: 20,
          background: tab === "receivable" ? "#dcfce7" : "#fef3c7",
          color: tab === "receivable" ? "#166534" : "#92400e",
          fontSize: 13, fontWeight: 600
        }}>
          Total: {fmt(total)}
        </div>
      </div>

      {loading ? <p style={{ color: "#aaa" }}>Loading...</p> : (
        <div style={{ background: "#fff", borderRadius: 12, border: "1px solid #f0f0f0" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ color: "#bbb", fontSize: 11, textTransform: "uppercase" }}>
                {["Party", "Voucher #", "Date", "Due Date", "Days Overdue", "Amount"].map(h => (
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
              {filtered.map((o, i) => (
                <tr key={i} style={{ borderBottom: "1px solid #fafafa" }}>
                  <td style={{ padding: "11px 20px", fontWeight: 500 }}>{o.party_name}</td>
                  <td style={{ padding: "11px 20px", color: "#888" }}>{o.voucher_number || "—"}</td>
                  <td style={{ padding: "11px 20px", color: "#888" }}>{o.voucher_date || "—"}</td>
                  <td style={{ padding: "11px 20px", color: "#888" }}>{o.due_date || "—"}</td>
                  <td style={{ padding: "11px 20px" }}>
                    <span style={{ color: agingColor(o.days_overdue || 0), fontWeight: 500 }}>
                      {o.days_overdue || 0} days
                    </span>
                  </td>
                  <td style={{
                    padding: "11px 20px", textAlign: "right", fontWeight: 600,
                    color: tab === "receivable" ? "#16a34a" : "#dc2626"
                  }}>
                    {fmt(o.pending_amount)}
                  </td>
                </tr>
              ))}
              {filtered.length === 0 && (
                <tr><td colSpan={6} style={{ padding: 32, textAlign: "center", color: "#ccc" }}>
                  No {tab} entries found
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
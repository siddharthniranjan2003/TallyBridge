import { useEffect, useState } from "react";
import { get, fmt } from "../api/client";

export default function Parties() {
  const [parties, setParties] = useState<any[]>([]);
  const [tab, setTab] = useState<"customer" | "supplier">("customer");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    get("/api/sync/parties")
      .then(r => setParties(r.parties || []))
      .finally(() => setLoading(false));
  }, []);

  const filtered = parties.filter(p => p.type === tab);

  return (
    <div style={{ padding: 32 }}>
      <h1 style={{ fontSize: 20, fontWeight: 600, marginBottom: 6 }}>Parties</h1>

      <div style={{ display: "flex", gap: 8, marginBottom: 20 }}>
        {(["customer", "supplier"] as const).map(t => (
          <button key={t} onClick={() => setTab(t)} style={{
            padding: "7px 18px", borderRadius: 20, fontSize: 13, cursor: "pointer",
            border: tab === t ? "none" : "1px solid #e5e7eb",
            background: tab === t ? "#1a1a2e" : "#fff",
            color: tab === t ? "#fff" : "#666", fontWeight: 500,
          }}>
            {t === "customer" ? "Customers" : "Suppliers"}
          </button>
        ))}
        <span style={{ marginLeft: "auto", fontSize: 13, color: "#888", alignSelf: "center" }}>
          {filtered.length} {tab}s
        </span>
      </div>

      {loading ? <p style={{ color: "#aaa" }}>Loading...</p> : (
        <div style={{ background: "#fff", borderRadius: 12, border: "1px solid #f0f0f0" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ color: "#bbb", fontSize: 11, textTransform: "uppercase" }}>
                {["Party Name", "Type", "Outstanding"].map(h => (
                  <th key={h} style={{
                    padding: "10px 20px",
                    textAlign: h === "Outstanding" ? "right" : "left",
                    fontWeight: 500, borderBottom: "1px solid #f5f5f5"
                  }}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {filtered.map((p, i) => (
                <tr key={i} style={{ borderBottom: "1px solid #fafafa" }}>
                  <td style={{ padding: "12px 20px", fontWeight: 500 }}>{p.name}</td>
                  <td style={{ padding: "12px 20px" }}>
                    <span style={{
                      fontSize: 11, padding: "2px 8px", borderRadius: 20, fontWeight: 500,
                      background: p.type === "customer" ? "#dbeafe" : "#fef3c7",
                      color: p.type === "customer" ? "#1e40af" : "#92400e",
                    }}>
                      {p.type}
                    </span>
                  </td>
                  <td style={{
                    padding: "12px 20px", textAlign: "right", fontWeight: 600,
                    color: p.total_outstanding > 0 ? "#dc2626" : "#888"
                  }}>
                    {p.total_outstanding > 0 ? fmt(p.total_outstanding) : "—"}
                  </td>
                </tr>
              ))}
              {filtered.length === 0 && (
                <tr><td colSpan={3} style={{ padding: 32, textAlign: "center", color: "#ccc" }}>
                  No {tab}s found
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
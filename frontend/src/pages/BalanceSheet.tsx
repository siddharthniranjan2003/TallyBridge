import { useEffect, useState } from "react";
import { get, fmt } from "../api/client";

export default function BalanceSheet() {
  const [data, setData] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    get("/api/sync/balance-sheet")
      .then(r => setData(r.balance_sheet || []))
      .finally(() => setLoading(false));
  }, []);

  const assets = data.filter(d => d.side === "asset");
  const liabilities = data.filter(d => d.side === "liability");
  const totalAssets = assets.reduce((s, d) => s + (d.amount || 0), 0);
  const totalLiab = liabilities.reduce((s, d) => s + (d.amount || 0), 0);

  return (
    <div style={{ padding: 32 }}>
      <h1 style={{ fontSize: 20, fontWeight: 600, marginBottom: 20 }}>Balance Sheet</h1>

      {loading ? <p style={{ color: "#aaa" }}>Loading...</p> : (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 20 }}>
          {[
            { title: "Assets", items: assets, total: totalAssets, color: "#2563eb" },
            { title: "Liabilities", items: liabilities, total: totalLiab, color: "#d97706" },
          ].map(section => (
            <div key={section.title} style={{ background: "#fff", borderRadius: 12, border: "1px solid #f0f0f0" }}>
              <div style={{
                padding: "14px 20px", borderBottom: "1px solid #f5f5f5",
                display: "flex", justifyContent: "space-between", alignItems: "center"
              }}>
                <span style={{ fontWeight: 500, color: section.color }}>{section.title}</span>
                <span style={{ fontSize: 13, fontWeight: 600, color: section.color }}>
                  {fmt(section.total)}
                </span>
              </div>
              {section.items.map((d, i) => (
                <div key={i} style={{
                  display: "flex", justifyContent: "space-between",
                  padding: "10px 20px", borderBottom: "1px solid #fafafa",
                  fontSize: 13
                }}>
                  <span style={{ color: "#555" }}>{d.particulars}</span>
                  <span style={{ fontWeight: 500 }}>{fmt(d.amount)}</span>
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

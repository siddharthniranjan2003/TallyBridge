import { useEffect, useState } from "react";
import { get, fmt } from "../api/client";

export default function ProfitLoss() {
  const [data, setData] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    get("/api/sync/pnl")
      .then(r => setData(r.profit_loss || []))
      .finally(() => setLoading(false));
  }, []);

  const income = data.filter(d => d.side === "credit").reduce((s, d) => s + (d.amount || 0), 0);
  const expenses = data.filter(d => d.side === "debit").reduce((s, d) => s + (d.amount || 0), 0);
  const profit = income - expenses;

  return (
    <div style={{ padding: 32 }}>
      <h1 style={{ fontSize: 20, fontWeight: 600, marginBottom: 20 }}>Profit &amp; Loss</h1>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(3,1fr)", gap: 14, marginBottom: 28 }}>
        {[
          { label: "Total Income", value: fmt(income), color: "#16a34a" },
          { label: "Total Expenses", value: fmt(expenses), color: "#dc2626" },
          {
            label: profit >= 0 ? "Net Profit" : "Net Loss",
            value: fmt(profit), color: profit >= 0 ? "#16a34a" : "#dc2626"
          },
        ].map(m => (
          <div key={m.label} style={{
            background: "#fff", borderRadius: 12,
            border: "1px solid #f0f0f0", padding: "16px 20px"
          }}>
            <p style={{ fontSize: 11, color: "#aaa", textTransform: "uppercase", marginBottom: 6 }}>{m.label}</p>
            <p style={{ fontSize: 22, fontWeight: 600, color: m.color }}>{m.value}</p>
          </div>
        ))}
      </div>

      {loading ? <p style={{ color: "#aaa" }}>Loading...</p> : (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 20 }}>
          {[
            { title: "Income", items: data.filter(d => d.side === "credit"), color: "#16a34a" },
            { title: "Expenses", items: data.filter(d => d.side === "debit"), color: "#dc2626" },
          ].map(section => (
            <div key={section.title} style={{ background: "#fff", borderRadius: 12, border: "1px solid #f0f0f0" }}>
              <div style={{
                padding: "14px 20px", borderBottom: "1px solid #f5f5f5",
                fontWeight: 500, color: section.color
              }}>
                {section.title}
              </div>
              {section.items.map((d, i) => (
                <div key={i} style={{
                  display: "flex", justifyContent: "space-between",
                  padding: "10px 20px", borderBottom: "1px solid #fafafa",
                  fontSize: 13
                }}>
                  <span style={{ color: "#555" }}>{d.account_name}</span>
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
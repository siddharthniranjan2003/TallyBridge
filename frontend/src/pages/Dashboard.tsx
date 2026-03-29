import { useEffect, useState } from "react";
import { get, fmt, COMPANY } from "../api/client";
import MetricCard from "../components/MetricCard";

const TYPE_COLORS: Record<string, string> = {
  Sales: "#dcfce7", Purchase: "#fef3c7",
  Receipt: "#dbeafe", Payment: "#fce7f3",
};
const TYPE_TEXT: Record<string, string> = {
  Sales: "#166534", Purchase: "#92400e",
  Receipt: "#1e40af", Payment: "#9d174d",
};

export default function Dashboard() {
  const [vouchers, setVouchers] = useState<any[]>([]);
  const [outstanding, setOutstanding] = useState<any[]>([]);
  const [stock, setStock] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      get("/api/sync/vouchers"),
      get("/api/sync/outstanding"),
      get("/api/sync/stock"),
    ]).then(([v, o, s]) => {
      setVouchers(v.vouchers || []);
      setOutstanding(o.outstanding || []);
      setStock(s.stock_items || []);
    }).finally(() => setLoading(false));
  }, []);

  if (loading) return (
    <div style={{ padding: 40, color: "#aaa", fontSize: 14 }}>Loading dashboard...</div>
  );

  const today = new Date().toISOString().split("T")[0];
  const todaySales = vouchers.filter(v => v.voucher_type === "Sales" && v.date === today)
    .reduce((s, v) => s + Math.abs(v.amount || 0), 0);
  const todayPurchases = vouchers.filter(v => v.voucher_type === "Purchase" && v.date === today)
    .reduce((s, v) => s + Math.abs(v.amount || 0), 0);
  const totalReceivable = outstanding.filter(o => o.type === "receivable")
    .reduce((s, o) => s + Math.abs(o.pending_amount || 0), 0);
  const totalPayable = outstanding.filter(o => o.type === "payable")
    .reduce((s, o) => s + Math.abs(o.pending_amount || 0), 0);
  const stockValue = stock.reduce((s, i) => s + Math.abs(i.closing_value || 0), 0);

  const recent = [...vouchers]
    .sort((a, b) => new Date(b.date).getTime() - new Date(a.date).getTime())
    .slice(0, 10);

  return (
    <div style={{ padding: 32 }}>
      {/* Header */}
      <div style={{ marginBottom: 24 }}>
        <h1 style={{ fontSize: 20, fontWeight: 600, color: "#1a1a2e" }}>{COMPANY}</h1>
        <p style={{ fontSize: 13, color: "#aaa", marginTop: 2 }}>
          {new Date().toLocaleDateString("en-IN", { weekday: "long", day: "numeric", month: "long", year: "numeric" })}
        </p>
      </div>

      {/* Metrics */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 14, marginBottom: 28 }}>
        <MetricCard label="Today's Sales" value={fmt(todaySales)} color="#16a34a" />
        <MetricCard label="Today's Purchases" value={fmt(todayPurchases)} color="#dc2626" />
        <MetricCard label="Total Receivable" value={fmt(totalReceivable)} color="#2563eb" />
        <MetricCard label="Total Payable" value={fmt(totalPayable)} color="#d97706" />
        <MetricCard label="Stock Value" value={fmt(stockValue)} color="#7c3aed" />
      </div>

      {/* Recent Transactions */}
      <div style={{ background: "#fff", borderRadius: 12, border: "1px solid #f0f0f0" }}>
        <div style={{ padding: "16px 20px", borderBottom: "1px solid #f9f9f9" }}>
          <h2 style={{ fontSize: 15, fontWeight: 500 }}>Recent transactions</h2>
        </div>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
          <thead>
            <tr style={{ color: "#bbb", fontSize: 11, textTransform: "uppercase" }}>
              {["Date", "Type", "Voucher #", "Party", "Amount"].map(h => (
                <th key={h} style={{
                  padding: "10px 20px", textAlign: h === "Amount" ? "right" : "left",
                  fontWeight: 500, borderBottom: "1px solid #f5f5f5"
                }}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {recent.map((v, i) => (
              <tr key={i} style={{ borderBottom: "1px solid #fafafa" }}>
                <td style={{ padding: "11px 20px", color: "#888" }}>{v.date}</td>
                <td style={{ padding: "11px 20px" }}>
                  <span style={{
                    fontSize: 11, padding: "2px 8px", borderRadius: 20, fontWeight: 500,
                    background: TYPE_COLORS[v.voucher_type] || "#f3f4f6",
                    color: TYPE_TEXT[v.voucher_type] || "#374151",
                  }}>
                    {v.voucher_type}
                  </span>
                </td>
                <td style={{ padding: "11px 20px", color: "#888" }}>{v.voucher_number || "—"}</td>
                <td style={{ padding: "11px 20px", color: "#374151" }}>{v.party_name || "—"}</td>
                <td style={{ padding: "11px 20px", textAlign: "right", fontWeight: 500 }}>
                  {fmt(v.amount)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
interface Props {
  label: string;
  value: string;
  color?: string;
  sub?: string;
}

export default function MetricCard({ label, value, color = "#185FA5", sub }: Props) {
  return (
    <div style={{
      background: "#fff", borderRadius: 12,
      border: "1px solid #f0f0f0", padding: "16px 20px",
    }}>
      <p style={{ fontSize: 11, color: "#aaa", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 6 }}>
        {label}
      </p>
      <p style={{ fontSize: 22, fontWeight: 600, color }}>{value}</p>
      {sub && <p style={{ fontSize: 11, color: "#bbb", marginTop: 4 }}>{sub}</p>}
    </div>
  );
}
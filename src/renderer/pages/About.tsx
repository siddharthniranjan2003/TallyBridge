export default function About() {
  return (
    <div style={{ padding: 28, maxWidth: 440 }}>
      <h1 style={{ fontSize: 20, fontWeight: 600, marginBottom: 6 }}>TallyBridge</h1>
      <p style={{ fontSize: 13, color: "#6c757d", marginBottom: 28 }}>
        TallyPrime cloud sync connector
      </p>

      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {[
          ["Version", "1.0.0"],
          ["Built with", "Electron · React · TypeScript · Python"],
          ["TallyPrime API", "XML over HTTP (localhost:9000)"],
          ["Support", "support@yourcompany.com"],
        ].map(([label, value]) => (
          <div key={label} style={{
            display: "flex", justifyContent: "space-between",
            padding: "12px 0", borderBottom: "1px solid #f1f3f5",
            fontSize: 13,
          }}>
            <span style={{ color: "#6c757d" }}>{label}</span>
            <span style={{ fontWeight: 500 }}>{value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
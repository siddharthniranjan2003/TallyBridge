import { useEffect, useState } from "react";

export default function Settings() {
  const [form, setForm] = useState({
    tallyUrl: "http://localhost:9000",
    syncIntervalMinutes: 5,
    backendUrl: "",
    apiKey: "",
    accountEmail: "",
    readMode: "auto",
    odbcDsnOverride: "",
  });
  const [saved, setSaved] = useState(false);
  const [tallyOk, setTallyOk] = useState<boolean | null>(null);
  const [capabilities, setCapabilities] = useState<any | null>(null);

  useEffect(() => {
    window.electronAPI.getConfig().then((cfg: any) => {
      setForm({
        tallyUrl: cfg.tallyUrl || "http://localhost:9000",
        syncIntervalMinutes: cfg.syncIntervalMinutes || 5,
        backendUrl: cfg.backendUrl || "",
        apiKey: cfg.apiKey || "",
        accountEmail: cfg.accountEmail || "",
        readMode: cfg.readMode || "auto",
        odbcDsnOverride: cfg.odbcDsnOverride || "",
      });
    });
  }, []);

  const set = (key: string, val: any) => setForm((f) => ({ ...f, [key]: val }));

  const handleSave = async () => {
    await window.electronAPI.saveSettings(form);
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  };

  const testTally = async () => {
    setTallyOk(null);
    const r = await window.electronAPI.checkTally();
    setTallyOk(r.connected);
  };

  const checkCapabilities = async () => {
    const result = await window.electronAPI.checkTallyCapabilities();
    setCapabilities(result);
  };

  return (
    <div style={{ padding: 28, maxWidth: 500 }}>
      <h1 style={{ fontSize: 20, fontWeight: 600, marginBottom: 28 }}>Settings</h1>

      <Section title="TallyPrime Connection">
        <Field label="Tally URL" hint="Default: http://localhost:9000">
          <div style={{ display: "flex", gap: 8 }}>
            <input value={form.tallyUrl} onChange={(e) => set("tallyUrl", e.target.value)} />
            <button onClick={testTally} style={testBtn}>Test</button>
          </div>
          {tallyOk === true && <p style={{ color: "#22c55e", fontSize: 12, marginTop: 4 }}>✓ Connected to TallyPrime</p>}
          {tallyOk === false && <p style={{ color: "#ef4444", fontSize: 12, marginTop: 4 }}>✗ Cannot connect — is TallyPrime open?</p>}
        </Field>

        <Field label="Sync Interval" hint="How often to sync (1–60 minutes)">
          <select
            value={form.syncIntervalMinutes}
            onChange={(e) => set("syncIntervalMinutes", Number(e.target.value))}
            style={{ width: "100%", padding: "9px 12px", borderRadius: 8, border: "1px solid #dee2e6", background: "#fff" }}
          >
            {[1, 2, 5, 10, 15, 30, 60].map((m) => (
              <option key={m} value={m}>{m} {m === 1 ? "minute" : "minutes"}</option>
            ))}
          </select>
        </Field>
        <Field label="Read Mode" hint="Auto prefers ODBC for supported ERP 9 master reads and falls back to XML">
          <select
            value={form.readMode}
            onChange={(e) => set("readMode", e.target.value)}
            style={{ width: "100%", padding: "9px 12px", borderRadius: 8, border: "1px solid #dee2e6", background: "#fff" }}
          >
            <option value="auto">Auto</option>
            <option value="xml-only">XML only</option>
            <option value="hybrid">Hybrid</option>
          </select>
        </Field>

        <Field label="ODBC DSN Override" hint="Optional. Leave blank unless you know the exact Tally ODBC DSN name.">
          <input
            value={form.odbcDsnOverride}
            onChange={(e) => set("odbcDsnOverride", e.target.value)}
            placeholder="TallyODBC_9000"
          />
        </Field>

        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <button onClick={checkCapabilities} style={testBtn}>Check Capabilities</button>
          {capabilities && (
            <span style={{ fontSize: 12, color: "#6b7280" }}>
              XML: {capabilities.xml?.connected ? "OK" : "Down"} | ODBC: {capabilities.odbc?.state || "unknown"}
            </span>
          )}
        </div>
        {capabilities && (
          <div style={{
            border: "1px solid #e5e7eb",
            borderRadius: 8,
            padding: 12,
            background: "#fff",
            fontSize: 12,
            color: "#374151",
          }}>
            <div>Transport plan: vouchers and reports use XML; masters use {capabilities.odbc?.state === "ok" ? "ODBC first" : "XML"}.</div>
            {capabilities.odbc?.dsn && <div>ODBC DSN: {capabilities.odbc.dsn}</div>}
            {capabilities.odbc?.message && <div>ODBC: {capabilities.odbc.message}</div>}
            {capabilities.xml?.error && <div>XML: {capabilities.xml.error}</div>}
          </div>
        )}
      </Section>

      <Section title="Cloud Account">
        <Field label="Backend URL" hint="Your Render deployment URL">
          <input
            value={form.backendUrl}
            onChange={(e) => set("backendUrl", e.target.value)}
            placeholder="https://your-app.onrender.com"
          />
        </Field>
        <Field label="API Key" hint="Keep this secret — never share it">
          <input
            type="password"
            value={form.apiKey}
            onChange={(e) => set("apiKey", e.target.value)}
            placeholder="••••••••••••"
          />
        </Field>
        <Field label="Account Email">
          <input
            type="email"
            value={form.accountEmail}
            onChange={(e) => set("accountEmail", e.target.value)}
            placeholder="you@example.com"
          />
        </Field>
      </Section>

      <button onClick={handleSave} style={{ ...primaryBtn, width: "100%" }}>
        {saved ? "✓ Settings Saved!" : "Save Settings"}
      </button>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 28 }}>
      <p style={{ fontSize: 11, fontWeight: 600, color: "#adb5bd", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 14 }}>
        {title}
      </p>
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {children}
      </div>
    </div>
  );
}

function Field({ label, hint, children }: any) {
  return (
    <div>
      <label style={{ display: "block", fontSize: 13, fontWeight: 500, marginBottom: 5, color: "#374151" }}>
        {label}
      </label>
      {children}
      {hint && <p style={{ fontSize: 11, color: "#adb5bd", marginTop: 4 }}>{hint}</p>}
    </div>
  );
}

const primaryBtn: React.CSSProperties = {
  background: "#1a1a2e", color: "#fff", border: "none",
  borderRadius: 8, padding: "11px 20px", cursor: "pointer",
  fontSize: 14, fontWeight: 500,
};
const testBtn: React.CSSProperties = {
  background: "transparent", border: "1px solid #dee2e6",
  borderRadius: 8, padding: "9px 14px", cursor: "pointer",
  fontSize: 13, whiteSpace: "nowrap", flexShrink: 0,
};

import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";

type Step = "loading" | "select" | "noTally" | "checking" | "success" | "error";

export default function AddCompany() {
  const [step, setStep] = useState<Step>("loading");
  const [companies, setCompanies] = useState<string[]>([]);
  const [selected, setSelected] = useState("");
  const [errorMsg, setErrorMsg] = useState("");
  const navigate = useNavigate();

  // Auto-fetch companies when page loads
  useEffect(() => {
    fetchCompanies();
  }, []);

  const fetchCompanies = async () => {
    setStep("loading");
    const result = await window.electronAPI.getTallyCompanies();
    if (result.success && result.companies.length > 0) {
      setCompanies(result.companies);
      setSelected(result.companies[0]);
      setStep("select");
    } else {
      setStep("noTally");
    }
  };

  const handleAdd = async () => {
    if (!selected) return;
    setStep("checking");
    const result = await window.electronAPI.addCompany(selected);
    if (result.success) {
      setStep("success");
    } else {
      setErrorMsg(result.error || "Unknown error");
      setStep("error");
    }
  };

  return (
    <div style={{ padding: 28, maxWidth: 480 }}>
      <button onClick={() => navigate("/")} style={backBtn}>← Back</button>
      <h1 style={{ fontSize: 20, fontWeight: 600, margin: "16px 0 6px" }}>Add Company</h1>
      <p style={{ fontSize: 13, color: "#6c757d", marginBottom: 28 }}>
        Companies currently open in TallyPrime on this PC.
      </p>

      {/* Loading */}
      {step === "loading" && (
        <CentreState icon="⏳" title="Detecting companies..." subtitle="Reading from TallyPrime..." />
      )}

      {/* Tally not running */}
      {step === "noTally" && (
        <CentreState
          icon="⚠️"
          title="TallyPrime not detected"
          subtitle="Open TallyPrime and load your company, then try again."
          error
          action={
            <button onClick={fetchCompanies} style={primaryBtn}>
              ↺ Retry
            </button>
          }
        />
      )}

      {/* Company selector */}
      {step === "select" && (
        <>
          <div style={infoBox}>
            <p style={{ fontWeight: 500, marginBottom: 6, fontSize: 13 }}>
              ✓ TallyPrime is connected
            </p>
            <p style={{ fontSize: 12, color: "#6c757d" }}>
              {companies.length} {companies.length === 1 ? "company" : "companies"} found
            </p>
          </div>

          <label style={labelStyle}>Select Company</label>

          {/* Company list — like BizAnalyst */}
          <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 20 }}>
            {companies.map((name) => (
              <div
                key={name}
                onClick={() => setSelected(name)}
                style={{
                  padding: "12px 16px",
                  borderRadius: 10,
                  border: `2px solid ${selected === name ? "#1a1a2e" : "#e9ecef"}`,
                  background: selected === name ? "#f0f0f5" : "#fff",
                  cursor: "pointer",
                  display: "flex",
                  alignItems: "center",
                  gap: 12,
                  transition: "all 0.15s",
                }}
              >
                <span style={{ fontSize: 20 }}>🏢</span>
                <div>
                  <p style={{ fontWeight: 500, fontSize: 14, margin: 0 }}>{name}</p>
                  <p style={{ fontSize: 11, color: "#6c757d", margin: 0 }}>TallyPrime</p>
                </div>
                {selected === name && (
                  <span style={{ marginLeft: "auto", color: "#1a1a2e", fontSize: 16 }}>✓</span>
                )}
              </div>
            ))}
          </div>

          <div style={{ display: "flex", gap: 8 }}>
            <button onClick={fetchCompanies} style={{ ...outlineBtn, flex: 1 }}>
              ↺ Refresh
            </button>
            <button
              onClick={handleAdd}
              disabled={!selected}
              style={{ ...primaryBtn, flex: 2, opacity: selected ? 1 : 0.5 }}
            >
              Add {selected ? `"${selected}"` : "Company"} →
            </button>
          </div>
        </>
      )}

      {step === "checking" && (
        <CentreState icon="⏳" title="Adding company..." subtitle={`Verifying "${selected}" in TallyPrime...`} />
      )}

      {step === "success" && (
        <CentreState
          icon="✅"
          title={`${selected} added!`}
          subtitle="First sync will start automatically in a few seconds."
          action={<button onClick={() => navigate("/")} style={primaryBtn}>Go to Home →</button>}
        />
      )}

      {step === "error" && (
        <CentreState
          icon="❌"
          title="Could not add company"
          subtitle={errorMsg}
          error
          action={
            <div style={{ display: "flex", gap: 10 }}>
              <button onClick={() => setStep("select")} style={outlineBtn}>Try Again</button>
              <button onClick={() => navigate("/settings")} style={primaryBtn}>Check Settings</button>
            </div>
          }
        />
      )}
    </div>
  );
}

function CentreState({ icon, title, subtitle, action, error }: any) {
  return (
    <div style={{ textAlign: "center", padding: "40px 0" }}>
      <div style={{ fontSize: 44, marginBottom: 14 }}>{icon}</div>
      <p style={{ fontSize: 16, fontWeight: 500, color: error ? "#ef4444" : "#1a1a2e", marginBottom: 6 }}>{title}</p>
      <p style={{ fontSize: 13, color: "#6c757d", marginBottom: action ? 24 : 0 }}>{subtitle}</p>
      {action}
    </div>
  );
}

const primaryBtn: React.CSSProperties = {
  background: "#1a1a2e", color: "#fff", border: "none",
  borderRadius: 8, padding: "10px 20px", cursor: "pointer",
  fontSize: 13, fontWeight: 500, textAlign: "center",
};
const outlineBtn: React.CSSProperties = {
  background: "transparent", color: "#1a1a2e",
  border: "1px solid #1a1a2e", borderRadius: 8,
  padding: "10px 20px", cursor: "pointer", fontSize: 13,
  textAlign: "center",
};
const backBtn: React.CSSProperties = {
  background: "none", border: "none", cursor: "pointer",
  color: "#6c757d", fontSize: 13, padding: 0,
};
const labelStyle: React.CSSProperties = {
  display: "block", fontSize: 13, fontWeight: 500,
  marginBottom: 10, color: "#374151",
};
const infoBox: React.CSSProperties = {
  background: "#f0fdf4", borderRadius: 10,
  padding: "12px 16px", marginBottom: 20,
  border: "1px solid #bbf7d0",
};
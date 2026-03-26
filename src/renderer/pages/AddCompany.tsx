import { useState } from "react";
import { useNavigate } from "react-router-dom";

type Step = "form" | "checking" | "success" | "error";

export default function AddCompany() {
  const [step, setStep] = useState<Step>("form");
  const [name, setName] = useState("");
  const [errorMsg, setErrorMsg] = useState("");
  const navigate = useNavigate();

  const handleSubmit = async () => {
    if (!name.trim()) return;
    setStep("checking");
    const result = await window.electronAPI.addCompany(name.trim());
    if (result.success) {
      setStep("success");
    } else {
      setErrorMsg(result.error);
      setStep("error");
    }
  };

  return (
    <div style={{ padding: 28, maxWidth: 480 }}>
      <button onClick={() => navigate("/")} style={backBtn}>← Back</button>
      <h1 style={{ fontSize: 20, fontWeight: 600, margin: "16px 0 6px" }}>Add Company</h1>
      <p style={{ fontSize: 13, color: "#6c757d", marginBottom: 28 }}>
        Connect a TallyPrime company to start syncing its data to the cloud.
      </p>

      {step === "form" && (
        <>
          {/* Instructions box */}
          <div style={infoBox}>
            <p style={{ fontWeight: 500, marginBottom: 8 }}>Before you add:</p>
            <ol style={{ paddingLeft: 18, lineHeight: 2, color: "#495057" }}>
              <li>Open <strong>TallyPrime</strong> on this PC</li>
              <li>Select your company — you should see <em>Gateway of Tally</em></li>
              <li>Note the company name shown at the top exactly</li>
            </ol>
          </div>

          <label style={labelStyle}>
            Company Name <span style={{ color: "#adb5bd", fontWeight: 400 }}>(exactly as in TallyPrime)</span>
          </label>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Demo Trading Co"
            onKeyDown={(e) => e.key === "Enter" && handleSubmit()}
            autoFocus
            style={{ marginBottom: 16 }}
          />

          <button
            onClick={handleSubmit}
            disabled={!name.trim()}
            style={{ ...primaryBtn, width: "100%", opacity: name.trim() ? 1 : 0.5 }}
          >
            Verify & Add Company →
          </button>
        </>
      )}

      {step === "checking" && (
        <CentreState icon="⏳" title="Verifying..." subtitle="Connecting to TallyPrime..." />
      )}

      {step === "success" && (
        <CentreState
          icon="✅"
          title={`${name} added!`}
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
              <button onClick={() => setStep("form")} style={outlineBtn}>Try Again</button>
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
  fontSize: 13, fontWeight: 500,
};
const outlineBtn: React.CSSProperties = {
  background: "transparent", color: "#1a1a2e",
  border: "1px solid #1a1a2e", borderRadius: 8,
  padding: "10px 20px", cursor: "pointer", fontSize: 13,
};
const backBtn: React.CSSProperties = {
  background: "none", border: "none", cursor: "pointer",
  color: "#6c757d", fontSize: 13, padding: 0,
};
const labelStyle: React.CSSProperties = {
  display: "block", fontSize: 13, fontWeight: 500,
  marginBottom: 6, color: "#374151",
};
const infoBox: React.CSSProperties = {
  background: "#f8f9fa", borderRadius: 10,
  padding: 16, marginBottom: 20, fontSize: 13,
  border: "1px solid #e9ecef",
};
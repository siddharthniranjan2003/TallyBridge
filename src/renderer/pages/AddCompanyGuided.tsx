import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

type Step = "loading" | "select" | "noTally" | "checking" | "success" | "error";

type TallyCompanyOption = {
  name: string;
  guid?: string;
  formalName?: string;
};

function getCompanyKey(company: TallyCompanyOption) {
  return company.guid || `${company.name}::${company.formalName || ""}`;
}

function getCompanySubtitle(company: TallyCompanyOption) {
  if (company.formalName && company.formalName !== company.name) {
    return company.formalName;
  }

  if (company.guid) {
    const shortGuid = company.guid.length > 12 ? company.guid.slice(-12) : company.guid;
    return `GUID: ${shortGuid}`;
  }

  return "TallyPrime";
}

export default function AddCompanyGuided() {
  const [step, setStep] = useState<Step>("loading");
  const [companies, setCompanies] = useState<TallyCompanyOption[]>([]);
  const [selectedKey, setSelectedKey] = useState("");
  const [errorMsg, setErrorMsg] = useState("");
  const navigate = useNavigate();

  useEffect(() => {
    void fetchCompanies();
  }, []);

  const selectedCompany = useMemo(
    () => companies.find((company) => getCompanyKey(company) === selectedKey),
    [companies, selectedKey],
  );

  const fetchCompanies = async () => {
    setStep("loading");
    const result = await window.electronAPI.getTallyCompanies();
    if (result.success && result.companies.length > 0) {
      setCompanies(result.companies);
      setSelectedKey(getCompanyKey(result.companies[0]));
      setStep("select");
      return;
    }

    setCompanies([]);
    setSelectedKey("");
    setStep("noTally");
  };

  const handleAdd = async () => {
    if (!selectedCompany) {
      return;
    }

    setStep("checking");
    const result = await window.electronAPI.addCompany(selectedCompany);
    if (result.success) {
      setStep("success");
      return;
    }

    setErrorMsg(result.error || "Unknown error");
    setStep("error");
  };

  return (
    <div style={{ padding: 28, maxWidth: 520 }}>
      <button onClick={() => navigate("/")} style={backBtn}>{"<-"} Back</button>
      <h1 style={{ fontSize: 20, fontWeight: 600, margin: "16px 0 6px" }}>Add Company</h1>
      <p style={{ fontSize: 13, color: "#6c757d", marginBottom: 28 }}>
        Companies currently open in TallyPrime on this PC.
      </p>

      {step === "loading" && (
        <CentreState icon="..." title="Detecting companies..." subtitle="Reading from TallyPrime..." />
      )}

      {step === "noTally" && (
        <CentreState
          icon="!"
          title="TallyPrime not detected"
          subtitle="Open TallyPrime and load your company, then try again."
          error
          action={
            <button onClick={() => void fetchCompanies()} style={primaryBtn}>
              Retry
            </button>
          }
        />
      )}

      {step === "select" && (
        <>
          <div style={infoBox}>
            <p style={{ fontWeight: 500, marginBottom: 6, fontSize: 13 }}>
              TallyPrime is connected
            </p>
            <p style={{ fontSize: 12, color: "#6c757d" }}>
              {companies.length} {companies.length === 1 ? "company" : "companies"} found
            </p>
          </div>

          <label style={labelStyle}>Select Company</label>

          <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 20 }}>
            {companies.map((company) => {
              const companyKey = getCompanyKey(company);
              const isSelected = selectedKey === companyKey;

              return (
                <div
                  key={companyKey}
                  onClick={() => setSelectedKey(companyKey)}
                  style={{
                    padding: "12px 16px",
                    borderRadius: 10,
                    border: `2px solid ${isSelected ? "#1a1a2e" : "#e9ecef"}`,
                    background: isSelected ? "#f0f0f5" : "#fff",
                    cursor: "pointer",
                    display: "flex",
                    alignItems: "center",
                    gap: 12,
                    transition: "all 0.15s",
                  }}
                >
                  <span style={{ fontSize: 20 }}>[]</span>
                  <div style={{ minWidth: 0 }}>
                    <p style={{ fontWeight: 500, fontSize: 14, margin: 0 }}>{company.name}</p>
                    <p style={{ fontSize: 11, color: "#6c757d", margin: 0 }}>
                      {getCompanySubtitle(company)}
                    </p>
                  </div>
                  {isSelected && (
                    <span style={{ marginLeft: "auto", color: "#1a1a2e", fontSize: 16 }}>OK</span>
                  )}
                </div>
              );
            })}
          </div>

          <div style={{ display: "flex", gap: 8 }}>
            <button onClick={() => void fetchCompanies()} style={{ ...outlineBtn, flex: 1 }}>
              Refresh
            </button>
            <button
              onClick={() => void handleAdd()}
              disabled={!selectedCompany}
              style={{ ...primaryBtn, flex: 2, opacity: selectedCompany ? 1 : 0.5 }}
            >
              Add {selectedCompany ? `"${selectedCompany.name}"` : "Company"}
            </button>
          </div>
        </>
      )}

      {step === "checking" && (
        <CentreState
          icon="..."
          title="Adding company..."
          subtitle={`Verifying "${selectedCompany?.name || ""}" in TallyPrime...`}
        />
      )}

      {step === "success" && (
        <CentreState
          icon="OK"
          title={`${selectedCompany?.name || "Company"} added!`}
          subtitle="First sync will start automatically in a few seconds."
          action={<button onClick={() => navigate("/")} style={primaryBtn}>Go to Home</button>}
        />
      )}

      {step === "error" && (
        <CentreState
          icon="X"
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

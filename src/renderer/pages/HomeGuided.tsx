import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Company } from "../../main/store";
import CompanyCardStable from "../components/CompanyCardStable";

export default function HomeGuided() {
  const [companies, setCompanies] = useState<Company[]>([]);
  const [isSyncing, setIsSyncing] = useState(false);
  const [isPaused, setIsPaused] = useState(false);
  const navigate = useNavigate();

  useEffect(() => {
    void (async () => {
      const cfg = await window.electronAPI.getConfig();
      setCompanies(cfg.companies || []);
      setIsPaused(Boolean(cfg.syncPaused));
    })();

    const onUpdated = (_: unknown, updated: Company[]) => setCompanies(updated);
    const onSyncStart = () => setIsSyncing(true);
    const onSyncComplete = () => setIsSyncing(false);
    const onSyncPaused = (_: unknown, { paused }: { paused: boolean }) => {
      setIsPaused(paused);
      if (paused) setIsSyncing(false);
    };

    window.electronAPI.on("companies-updated", onUpdated);
    window.electronAPI.on("sync-start", onSyncStart);
    window.electronAPI.on("sync-complete", onSyncComplete);
    window.electronAPI.on("sync-paused", onSyncPaused);

    return () => {
      window.electronAPI.off("companies-updated", onUpdated);
      window.electronAPI.off("sync-start", onSyncStart);
      window.electronAPI.off("sync-complete", onSyncComplete);
      window.electronAPI.off("sync-paused", onSyncPaused);
    };
  }, []);

  const handleSyncNow = async () => {
    setIsSyncing(true);
    await window.electronAPI.syncNow();
  };

  const handlePauseResume = async () => {
    if (isPaused) {
      await window.electronAPI.resumeSync();
      setIsPaused(false);
    } else {
      await window.electronAPI.pauseSync();
      setIsPaused(true);
      setIsSyncing(false);
    }
  };

  const handleRemove = async (id: string) => {
    await window.electronAPI.removeCompany(id);
  };

  const duplicateNameCounts = companies.reduce<Record<string, number>>((counts, company) => {
    const normalizedName = company.name.trim().toLowerCase();
    counts[normalizedName] = (counts[normalizedName] || 0) + 1;
    return counts;
  }, {});

  return (
    <div style={{ padding: 28 }}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 24,
        }}
      >
        <div>
          <h1 style={{ fontSize: 20, fontWeight: 600, marginBottom: 2 }}>My Companies</h1>
          <p style={{ fontSize: 13, color: "#6c757d" }}>
            {companies.length} {companies.length === 1 ? "company" : "companies"} connected
          </p>
        </div>
        <div style={{ display: "flex", gap: 10 }}>
          <button onClick={() => navigate("/add-company")} style={outlineBtn}>
            + Add Company
          </button>
          <button
            onClick={handlePauseResume}
            style={{
              ...outlineBtn,
              color: isPaused ? "#22c55e" : "#ef4444",
              borderColor: isPaused ? "#22c55e" : "#ef4444",
            }}
          >
            {isPaused ? "▶ Resume Sync" : "⏸ Pause Sync"}
          </button>
          <button
            onClick={handleSyncNow}
            disabled={isSyncing || isPaused}
            style={{ ...primaryBtn, opacity: (isSyncing || isPaused) ? 0.5 : 1 }}
          >
            {isSyncing ? "Syncing..." : "Sync All Now"}
          </button>
        </div>
      </div>

      {companies.length === 0 ? (
        <EmptyState onAdd={() => navigate("/add-company")} />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {companies.map((company) => (
            <CompanyCardStable
              key={company.id}
              company={company}
              onRemove={handleRemove}
              showIdentityHint={(duplicateNameCounts[company.name.trim().toLowerCase()] || 0) > 1}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function EmptyState({ onAdd }: { onAdd: () => void }) {
  return (
    <div
      style={{
        textAlign: "center",
        padding: "80px 20px",
        color: "#adb5bd",
      }}
    >
      <div style={{ fontSize: 52, marginBottom: 16 }}>[]</div>
      <p style={{ fontSize: 16, fontWeight: 500, color: "#6c757d", marginBottom: 6 }}>
        No companies added yet
      </p>
      <p style={{ fontSize: 13, marginBottom: 24 }}>
        Open TallyPrime on this PC, then add your company below.
      </p>
      <button onClick={onAdd} style={primaryBtn}>
        + Add Your First Company
      </button>
    </div>
  );
}

const primaryBtn: React.CSSProperties = {
  background: "#1a1a2e",
  color: "#fff",
  border: "none",
  borderRadius: 8,
  padding: "9px 18px",
  cursor: "pointer",
  fontSize: 13,
  fontWeight: 500,
};

const outlineBtn: React.CSSProperties = {
  background: "transparent",
  color: "#1a1a2e",
  border: "1px solid #1a1a2e",
  borderRadius: 8,
  padding: "9px 18px",
  cursor: "pointer",
  fontSize: 13,
};

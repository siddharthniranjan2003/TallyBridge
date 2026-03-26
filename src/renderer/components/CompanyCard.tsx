import { Company } from "../../main/store";

interface Props {
  company: Company;
  onRemove: (id: string) => void;
}

export default function CompanyCard({ company, onRemove }: Props) {
  const statusColors = {
    idle: "#94a3b8",
    syncing: "#f59e0b",
    success: "#22c55e",
    error: "#ef4444",
  };

  const statusLabels = {
    idle: "Not synced yet",
    syncing: "Syncing...",
    success: "Synced",
    error: "Failed",
  };

  const s = company.lastSyncStatus || "idle";

  const formatTime = (iso?: string) => {
    if (!iso) return "";
    return new Date(iso).toLocaleString("en-IN", {
      day: "2-digit", month: "short",
      hour: "2-digit", minute: "2-digit",
    });
  };

  return (
    <div style={{
      background: "#fff",
      border: "1px solid #e9ecef",
      borderRadius: 12,
      padding: "16px 18px",
      display: "flex",
      alignItems: "flex-start",
      justifyContent: "space-between",
      gap: 12,
    }}>
      <div style={{ flex: 1 }}>
        {/* Name + status */}
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
          <span style={{
            width: 9, height: 9,
            borderRadius: "50%",
            background: statusColors[s],
            display: "inline-block",
            flexShrink: 0,
            ...(s === "syncing" ? { animation: "pulse 1s infinite" } : {}),
          }} />
          <span style={{ fontWeight: 500, fontSize: 15 }}>{company.name}</span>
        </div>

        {/* Status line */}
        <div style={{ fontSize: 12, color: "#6c757d", marginLeft: 17 }}>
          {s === "syncing" && (
            <span style={{ color: "#f59e0b" }}>Syncing data from TallyPrime...</span>
          )}
          {s === "success" && company.lastSyncedAt && (
            <>
              <span style={{ color: "#22c55e" }}>Last synced: {formatTime(company.lastSyncedAt)}</span>
              {company.lastSyncRecords && (
                <span style={{ color: "#adb5bd", marginLeft: 8 }}>
                  · Ledgers: {company.lastSyncRecords.ledgers}
                  · Vouchers: {company.lastSyncRecords.vouchers}
                  · Stock: {company.lastSyncRecords.stock}
                  · Outstanding: {company.lastSyncRecords.outstanding}
                </span>
              )}
            </>
          )}
          {s === "error" && (
            <span style={{ color: "#ef4444" }}>
              Error: {company.lastSyncError || "Unknown error"}
            </span>
          )}
          {s === "idle" && (
            <span>Added {formatTime(company.addedAt)} · Click Sync All to start</span>
          )}
        </div>
      </div>

      {/* Remove button */}
      <button
        onClick={() => {
          if (confirm(`Remove ${company.name} from TallyBridge?`)) {
            onRemove(company.id);
          }
        }}
        style={{
          background: "none",
          border: "1px solid #dee2e6",
          borderRadius: 6,
          padding: "4px 10px",
          fontSize: 12,
          color: "#6c757d",
          cursor: "pointer",
          flexShrink: 0,
        }}
      >
        Remove
      </button>
    </div>
  );
}
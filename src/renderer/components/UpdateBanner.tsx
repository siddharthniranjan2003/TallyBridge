import { useEffect, useState } from "react";

type Phase = "available" | "downloading" | "downloaded" | "installing" | "error";

export default function UpdateBanner() {
  const [phase, setPhase] = useState<Phase | null>(null);
  const [version, setVersion] = useState("");
  const [percent, setPercent] = useState(0);
  const [error, setError] = useState("");
  const [deferred, setDeferred] = useState(false);
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    const onAvailable = (_: any, p: { version: string }) => {
      setVersion(p.version);
      setPhase("available");
      setDismissed(false);
    };
    const onProgress = (_: any, p: { percent: number }) => {
      setPercent(p.percent);
      setPhase("downloading");
    };
    const onDownloaded = (_: any, p: { version: string }) => {
      setVersion(p.version);
      setPhase("downloaded");
      setDismissed(false);
    };
    const onError = (_: any, p: { message: string }) => {
      // Only surface failures the user is waiting on (download/install).
      // Routine background check failures are non-actionable noise.
      setError(p.message);
      setPhase((prev) =>
        prev === "downloading" || prev === "installing" ? "error" : prev,
      );
    };

    window.electronAPI.on("update-available", onAvailable);
    window.electronAPI.on("update-progress", onProgress);
    window.electronAPI.on("update-downloaded", onDownloaded);
    window.electronAPI.on("update-error", onError);
    return () => {
      window.electronAPI.off("update-available", onAvailable);
      window.electronAPI.off("update-progress", onProgress);
      window.electronAPI.off("update-downloaded", onDownloaded);
      window.electronAPI.off("update-error", onError);
    };
  }, []);

  if (!phase || dismissed) return null;

  const isError = phase === "error";
  const bar: React.CSSProperties = {
    display: "flex",
    alignItems: "center",
    gap: 12,
    padding: "10px 16px",
    fontSize: 13,
    color: "#fff",
    background: isError ? "#b02a37" : "#1f6feb",
  };
  const button: React.CSSProperties = {
    padding: "5px 14px",
    borderRadius: 6,
    border: "none",
    fontSize: 13,
    fontWeight: 600,
    cursor: "pointer",
    background: "#fff",
    color: isError ? "#b02a37" : "#1f6feb",
  };
  const dismiss: React.CSSProperties = {
    marginLeft: "auto",
    background: "transparent",
    border: "none",
    color: "#fff",
    fontSize: 16,
    cursor: "pointer",
    lineHeight: 1,
  };

  return (
    <div style={bar}>
      {phase === "available" && (
        <>
          <span>Version {version} is available.</span>
          <button
            style={button}
            onClick={() => {
              setPhase("downloading");
              setPercent(0);
              void window.electronAPI.downloadUpdate();
            }}
          >
            Update now
          </button>
          <button style={dismiss} title="Dismiss" onClick={() => setDismissed(true)}>×</button>
        </>
      )}

      {phase === "downloading" && (
        <>
          <span>Downloading update… {percent}%</span>
          <div style={{ flex: 1, maxWidth: 240, height: 6, borderRadius: 3, background: "rgba(255,255,255,0.3)" }}>
            <div style={{ width: `${percent}%`, height: "100%", borderRadius: 3, background: "#fff" }} />
          </div>
        </>
      )}

      {phase === "downloaded" && (
        <>
          <span>Version {version} is ready to install.</span>
          <button
            style={button}
            onClick={async () => {
              setPhase("installing");
              const result = await window.electronAPI.installUpdate();
              setDeferred(result.deferred);
            }}
          >
            Restart to install
          </button>
          <button style={dismiss} title="Later" onClick={() => setDismissed(true)}>×</button>
        </>
      )}

      {phase === "installing" && (
        <span>
          {deferred
            ? "Update will install once the current sync finishes…"
            : "Restarting to install…"}
        </span>
      )}

      {phase === "error" && (
        <>
          <span>Update failed: {error}</span>
          <button style={dismiss} title="Dismiss" onClick={() => setDismissed(true)}>×</button>
        </>
      )}
    </div>
  );
}

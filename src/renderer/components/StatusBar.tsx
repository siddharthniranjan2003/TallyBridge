import { useEffect, useState } from "react";

export default function StatusBar() {
  const [tallyOk, setTallyOk] = useState(false);
  const [internetOk] = useState(true);
  const [nextSync, setNextSync] = useState("5:00");
  const [syncInterval, setSyncInterval] = useState(5);
  const [isPaused, setIsPaused] = useState(false);

  useEffect(() => {
    // Check Tally on mount and every 10s
    const checkTally = async () => {
      const r = await window.electronAPI.checkTally();
      setTallyOk(r.connected);
    };
    checkTally();
    const t = setInterval(checkTally, 10000);

    // Load interval from config
    window.electronAPI.getConfig().then((cfg: any) => {
      setSyncInterval(cfg.syncIntervalMinutes || 5);
      setIsPaused(Boolean(cfg.syncPaused));
    });

    const onSyncPaused = (_: unknown, { paused }: { paused: boolean }) => {
      setIsPaused(paused);
    };
    window.electronAPI.on("sync-paused", onSyncPaused);

    return () => {
      clearInterval(t);
      window.electronAPI.off("sync-paused", onSyncPaused);
    };
  }, []);

  // Countdown timer
  useEffect(() => {
    if (isPaused) {
      setNextSync("--:--");
      return;
    }

    let seconds = syncInterval * 60;
    const tick = setInterval(() => {
      seconds -= 1;
      if (seconds < 0) seconds = syncInterval * 60;
      const m = Math.floor(seconds / 60);
      const s = seconds % 60;
      setNextSync(`${m}:${s.toString().padStart(2, "0")}`);
    }, 1000);

    // Reset on sync
    const onSyncComplete = () => {
      seconds = syncInterval * 60;
    };
    window.electronAPI.on("sync-complete", onSyncComplete);

    return () => {
      clearInterval(tick);
      window.electronAPI.off("sync-complete", onSyncComplete);
    };
  }, [isPaused, syncInterval]);

  const dot = (on: boolean) => (
    <span style={{
      display: "inline-block",
      width: 7, height: 7,
      borderRadius: "50%",
      background: on ? "#22c55e" : "#ef4444",
      marginRight: 5,
    }} />
  );

  return (
    <div style={{
      background: "#1a1a2e",
      color: "rgba(255,255,255,0.7)",
      fontSize: 11,
      padding: "6px 20px",
      display: "flex",
      justifyContent: "space-between",
      alignItems: "center",
      flexShrink: 0,
    }}>
      <div style={{ display: "flex", gap: 20 }}>
        <span>{dot(tallyOk)} Tally: {tallyOk ? "CONNECTED" : "NOT RUNNING"}</span>
        <span>{dot(internetOk)} Internet: {internetOk ? "OK" : "OFFLINE"}</span>
      </div>
      <span>
        {isPaused ? "Auto-sync paused" : `Auto-sync every ${syncInterval}m · Next in ${nextSync}`}
      </span>
    </div>
  );
}

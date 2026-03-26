import { useEffect, useRef, useState } from "react";

interface LogEntry {
  time: string;
  company: string;
  line: string;
  isError: boolean;
}

export default function SyncLog() {
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handler = (_: any, data: { company: string; line: string }) => {
      setLogs((prev) => [
        ...prev.slice(-500),
        {
          time: new Date().toLocaleTimeString("en-IN"),
          company: data.company,
          line: data.line.trim(),
          isError: data.line.toLowerCase().includes("[err") || data.line.toLowerCase().includes("error"),
        },
      ]);
    };
    window.electronAPI.on("sync-log", handler);
    return () => window.electronAPI.off("sync-log", handler);
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs]);

  return (
    <div style={{ padding: 28 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <h1 style={{ fontSize: 20, fontWeight: 600 }}>Sync Log</h1>
        <button
          onClick={() => setLogs([])}
          style={{ background: "none", border: "1px solid #dee2e6", borderRadius: 6, padding: "5px 12px", fontSize: 12, cursor: "pointer", color: "#6c757d" }}
        >
          Clear
        </button>
      </div>

      <div style={{
        background: "#1a1a2e", borderRadius: 10, padding: 14,
        height: 420, overflowY: "auto",
        fontFamily: "'Consolas', 'Courier New', monospace",
        fontSize: 12, lineHeight: 1.7,
      }}>
        {logs.length === 0 ? (
          <span style={{ color: "#4a5568" }}>
            Waiting for sync activity... Sync runs automatically every few minutes.
          </span>
        ) : (
          logs.map((log, i) => (
            <div key={i} style={{ display: "flex", gap: 8 }}>
              <span style={{ color: "#4a5568", flexShrink: 0 }}>[{log.time}]</span>
              <span style={{ color: "#60a5fa", flexShrink: 0 }}>{log.company}</span>
              <span style={{ color: log.isError ? "#f87171" : "#a3e635" }}>{log.line}</span>
            </div>
          ))
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
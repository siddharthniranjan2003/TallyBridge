import { app } from "electron";
import fs from "fs";
import path from "path";

const MAX_LOG_BYTES = 5 * 1024 * 1024; // 5MB before rotation

let logPath: string | null = null;
let logStream: fs.WriteStream | null = null;

function getLogPath() {
  if (logPath) return logPath;
  const logsDir = app.getPath("logs");
  fs.mkdirSync(logsDir, { recursive: true });
  logPath = path.join(logsDir, "tallybridge.log");
  return logPath;
}

function rotateLogs() {
  const filePath = getLogPath();
  try {
    const stat = fs.statSync(filePath);
    if (stat.size >= MAX_LOG_BYTES) {
      const rotated = filePath.replace(".log", ".old.log");
      if (fs.existsSync(rotated)) fs.unlinkSync(rotated);
      fs.renameSync(filePath, rotated);
    }
  } catch {
    // file doesn't exist yet, no rotation needed
  }
}

function openStream() {
  if (logStream) return logStream;
  rotateLogs();
  logStream = fs.createWriteStream(getLogPath(), { flags: "a", encoding: "utf8" });
  return logStream;
}

function write(level: string, message: string) {
  const timestamp = new Date().toISOString();
  const line = `[${timestamp}] [${level}] ${message}\n`;
  process.stdout.write(line);
  try {
    openStream().write(line);
  } catch {
    // never crash the app because of a log write
  }
}

export const logger = {
  info: (message: string) => write("INFO", message),
  warn: (message: string) => write("WARN", message),
  error: (message: string) => write("ERROR", message),
  getLogPath,
};

export function initLogger() {
  const original = {
    log: console.log.bind(console),
    warn: console.warn.bind(console),
    error: console.error.bind(console),
  };

  console.log = (...args: unknown[]) => {
    const message = args.map(String).join(" ");
    write("INFO", message);
  };

  console.warn = (...args: unknown[]) => {
    const message = args.map(String).join(" ");
    write("WARN", message);
  };

  console.error = (...args: unknown[]) => {
    const message = args.map(String).join(" ");
    write("ERROR", message);
  };

  return original;
}

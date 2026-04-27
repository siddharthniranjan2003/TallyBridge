import { spawn } from "child_process";
import { app, BrowserWindow } from "electron";
import isDev from "electron-is-dev";
import path from "path";

import { Company, resolveControlPlaneApiKey, resolveControlPlaneUrl, store } from "./store";
import { SyncEngine } from "./sync-engine";

const DEFAULT_PUSH_QUEUE_POLL_INTERVAL_MS = 5000;
const INITIAL_PUSH_QUEUE_POLL_DELAY_MS = 5000;

function resolvePushQueuePollIntervalMs() {
  const parsed = Number((process.env.TB_PUSH_QUEUE_POLL_INTERVAL_MS || "").trim());
  if (Number.isInteger(parsed) && parsed >= 1000) {
    return parsed;
  }
  return DEFAULT_PUSH_QUEUE_POLL_INTERVAL_MS;
}

function resolvePythonCommand(scriptPath: string) {
  if (isDev) {
    if (process.platform === "win32") {
      return {
        command: "py",
        args: ["-3", "-u", scriptPath],
      };
    }
    return {
      command: "python3",
      args: ["-u", scriptPath],
    };
  }

  return {
    command: path.join(process.resourcesPath, "python-runtime", "tallybridge-engine.exe"),
    args: [],
  };
}

function isInterestingPollLine(line: string) {
  const trimmed = line.trim();
  if (!trimmed) {
    return false;
  }

  return ![
    "[Push] Checking backend queue for pending Sales/Purchase vouchers...",
    "[Push] No pending push jobs found.",
  ].includes(trimmed);
}

export class PushQueuePoller {
  private timer: NodeJS.Timeout | null = null;
  private stopped = false;

  constructor(
    private readonly mainWindow: BrowserWindow,
    private readonly syncEngine: SyncEngine,
  ) {}

  start() {
    if (this.timer) {
      return;
    }

    this.stopped = false;
    this.scheduleNext(INITIAL_PUSH_QUEUE_POLL_DELAY_MS);
  }

  stop() {
    this.stopped = true;
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  private scheduleNext(delayMs = resolvePushQueuePollIntervalMs()) {
    if (this.stopped) {
      return;
    }

    if (this.timer) {
      clearTimeout(this.timer);
    }

    this.timer = setTimeout(() => {
      this.timer = null;
      void this.pollOnce();
    }, delayMs);
  }

  private async pollOnce() {
    try {
      const config = store.store;
      const companies = store.get("companies").filter((company) => company.enabled);
      const controlPlaneUrl = resolveControlPlaneUrl(config);
      const controlPlaneApiKey = resolveControlPlaneApiKey(config);

      if (!controlPlaneUrl || !controlPlaneApiKey || !companies.length) {
        return;
      }

      if (this.syncEngine.isSyncInProgress()) {
        return;
      }

      for (const company of companies) {
        if (this.syncEngine.isSyncInProgress()) {
          break;
        }

        await this.pollCompanyQueue(company, {
          tallyUrl: config.tallyUrl,
          controlPlaneUrl,
          controlPlaneApiKey,
        });
      }
    } finally {
      this.scheduleNext();
    }
  }

  private pollCompanyQueue(
    company: Company,
    config: {
      tallyUrl: string;
      controlPlaneUrl: string;
      controlPlaneApiKey: string;
    },
  ) {
    return new Promise<void>((resolve) => {
      const scriptPath = isDev
        ? path.join(__dirname, "../../src/python/sync_main.py")
        : path.join(process.resourcesPath, "python", "sync_main.py");
      const pythonCommand = resolvePythonCommand(scriptPath);
      const env = {
        ...process.env,
        PYTHONUNBUFFERED: "1",
        TB_COMMAND: "poll_push_queue",
        TALLY_URL: config.tallyUrl,
        TALLY_COMPANY: company.name,
        TALLY_COMPANY_GUID: company.tallyGuid || "",
        BACKEND_URL: config.controlPlaneUrl,
        API_KEY: config.controlPlaneApiKey,
        CONTROL_PLANE_URL: config.controlPlaneUrl,
        CONTROL_PLANE_API_KEY: config.controlPlaneApiKey,
        TB_USER_DATA_DIR: app.getPath("userData"),
      };

      const proc = spawn(pythonCommand.command, pythonCommand.args, { env });
      let stdout = "";
      let stderr = "";

      proc.stdout?.on("data", (chunk: Buffer) => {
        stdout += chunk.toString();
      });

      proc.stderr?.on("data", (chunk: Buffer) => {
        stderr += chunk.toString();
      });

      proc.on("error", (error) => {
        this.log(company.name, `[Push] Queue poll failed to start: ${error.message}`);
        resolve();
      });

      proc.on("close", (code) => {
        const lines = stdout
          .split(/\r?\n/)
          .map((line) => line.trim())
          .filter(isInterestingPollLine);

        for (const line of lines) {
          this.log(company.name, line);
        }

        const stderrLines = stderr
          .split(/\r?\n/)
          .map((line) => line.trim())
          .filter(Boolean);
        for (const line of stderrLines) {
          this.log(company.name, `[ERR] ${line}`);
        }

        if (code && code !== 0 && !stderrLines.length) {
          this.log(company.name, `[Push] Queue poll exited with code ${code}.`);
        }

        resolve();
      });
    });
  }

  private log(company: string, line: string) {
    console.log(line);
    if (!this.mainWindow.isDestroyed()) {
      this.mainWindow.webContents.send("sync-log", {
        company,
        line,
      });
    }
  }
}

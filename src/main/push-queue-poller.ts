import { spawn, ChildProcess } from "child_process";
import { app, BrowserWindow } from "electron";
import isDev from "electron-is-dev";
import path from "path";

import { Company, resolveControlPlaneApiKey, resolveControlPlaneUrl, store } from "./store";
import { SyncEngine } from "./sync-engine";
import { shipSyncLog } from "./remote-log";
import { tallyGate } from "./tally-gate";

const DEFAULT_PUSH_QUEUE_POLL_INTERVAL_MS = 5000;
const INITIAL_PUSH_QUEUE_POLL_DELAY_MS = 5000;
// A push worker should finish quickly; if Tally is locked behind a modal/license
// popup the worker blocks in its HTTP read (~45s) holding the tally gate. Cap it
// so a wedged worker can't pin a process/the gate indefinitely.
const DEFAULT_PUSH_WORKER_TIMEOUT_MS = 90000;

function resolvePushQueuePollIntervalMs() {
  const parsed = Number((process.env.TB_PUSH_QUEUE_POLL_INTERVAL_MS || "").trim());
  if (Number.isInteger(parsed) && parsed >= 1000) {
    return parsed;
  }
  return DEFAULT_PUSH_QUEUE_POLL_INTERVAL_MS;
}

function resolvePushWorkerTimeoutMs() {
  const parsed = Number((process.env.TB_PUSH_WORKER_TIMEOUT_MS || "").trim());
  if (Number.isInteger(parsed) && parsed >= 5000) {
    return parsed;
  }
  return DEFAULT_PUSH_WORKER_TIMEOUT_MS;
}

// Force-kill a spawned engine process and its children. On Windows the
// PyInstaller exe spawns child processes that a plain SIGTERM leaves orphaned,
// so use taskkill /t to take down the whole tree (mirrors sync-engine).
function killProcessTree(proc: ChildProcess) {
  if (!proc.pid || proc.killed) {
    return;
  }
  try {
    if (process.platform === "win32") {
      spawn("taskkill", ["/pid", String(proc.pid), "/f", "/t"]);
    } else {
      proc.kill("SIGKILL");
    }
  } catch {
    // best-effort termination
  }
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
  // In-flight push worker processes, so they can be killed on quit/auto-update
  // instead of being orphaned (a leaked engine.exe holds RAM and contends with
  // Tally on port 9000 — costly on a 4GB box).
  private readonly activeChildren = new Set<ChildProcess>();

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
    for (const proc of this.activeChildren) {
      killProcessTree(proc);
    }
    this.activeChildren.clear();
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
      if (this.syncEngine.isPaused()) {
        return;
      }

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
        if (this.syncEngine.isPaused() || this.syncEngine.isSyncInProgress()) {
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
      // Don't start a push worker while anyone else owns single-threaded Tally —
      // a sync, the local-push :3002 worker, or a main-process request. Two
      // writers on :9000 crash it (c0000005). Skip this tick; the 5s poll
      // retries. This check is synchronous right before beginPythonWork() below,
      // so no other caller can slip in between.
      if (tallyGate.isBusy()) {
        resolve();
        return;
      }

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

      // A push worker writes vouchers into Tally — mark the gateway busy so the
      // status-bar connectivity probe won't fire a competing request meanwhile.
      tallyGate.beginPythonWork();
      let pythonWorkEnded = false;
      const endPythonWork = () => {
        if (!pythonWorkEnded) {
          pythonWorkEnded = true;
          tallyGate.endPythonWork();
        }
      };

      const proc = spawn(pythonCommand.command, pythonCommand.args, { env, windowsHide: true });
      this.activeChildren.add(proc);

      // Watchdog: terminate a push worker that runs too long (e.g. Tally is
      // locked behind a modal and the worker is stuck in its HTTP read) so it
      // can't pin the process or hold the tally gate forever.
      let settled = false;
      const watchdogMs = resolvePushWorkerTimeoutMs();
      let watchdog: NodeJS.Timeout | null = setTimeout(() => {
        watchdog = null;
        this.log(
          company.name,
          `[Push] Worker exceeded ${Math.round(watchdogMs / 1000)}s (Tally may be busy or locked) — terminating.`,
        );
        killProcessTree(proc);
        // Fallback settle: if 'close' never fires (taskkill couldn't fully reap,
        // or a surviving grandchild holds the stdout pipe), resolve anyway after
        // a grace period so pollOnce()'s await returns and the poller keeps
        // polling instead of wedging for the process lifetime. finish() is
        // idempotent, so a later 'close' is harmless.
        setTimeout(finish, 2000);
      }, watchdogMs);

      const cleanup = () => {
        if (watchdog) {
          clearTimeout(watchdog);
          watchdog = null;
        }
        this.activeChildren.delete(proc);
        endPythonWork();
      };

      const finish = () => {
        if (settled) {
          return;
        }
        settled = true;
        cleanup();
        resolve();
      };

      let stdout = "";
      let stderr = "";

      proc.stdout?.on("data", (chunk: Buffer) => {
        stdout += chunk.toString();
      });

      proc.stderr?.on("data", (chunk: Buffer) => {
        stderr += chunk.toString();
      });

      proc.on("error", (error) => {
        if (settled) {
          return;
        }
        this.log(company.name, `[Push] Queue poll failed to start: ${error.message}`);
        finish();
      });

      proc.on("close", (code) => {
        if (settled) {
          return; // watchdog fallback already settled this worker
        }
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

        finish();
      });
    });
  }

  private log(company: string, line: string) {
    console.log(line);
    shipSyncLog({ company, line });
    if (!this.mainWindow.isDestroyed()) {
      this.mainWindow.webContents.send("sync-log", {
        company,
        line,
      });
    }
  }
}

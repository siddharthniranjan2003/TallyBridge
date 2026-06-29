import { spawn, ChildProcess } from "child_process";
import { app, BrowserWindow, dialog } from "electron";
import { createServer, IncomingMessage, Server, ServerResponse } from "http";
import path from "path";
import isDev from "electron-is-dev";

import { store } from "./store";
import { shipSyncLog } from "./remote-log";
import { SyncEngine } from "./sync-engine";
import { tallyGate } from "./tally-gate";

const DEFAULT_LOCAL_PUSH_HOST = "127.0.0.1";
const DEFAULT_LOCAL_PUSH_PORT = 3002;
const MAX_REQUEST_BYTES = 1024 * 1024;
const DEFAULT_PUSH_WORKER_TIMEOUT_MS = 90000;

function resolvePushWorkerTimeoutMs() {
  const parsed = Number((process.env.TB_PUSH_WORKER_TIMEOUT_MS || "").trim());
  if (Number.isInteger(parsed) && parsed >= 5000) {
    return parsed;
  }
  return DEFAULT_PUSH_WORKER_TIMEOUT_MS;
}

// Force-kill a spawned engine process and its children (Windows taskkill /t
// takes down the PyInstaller exe's grandchildren that SIGTERM would orphan).
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

type PushVoucherPayload = Record<string, unknown> & {
  company_name?: unknown;
  company?: unknown;
};

function resolveLocalPushPort() {
  const parsed = Number((process.env.TB_LOCAL_PUSH_PORT || "").trim());
  if (Number.isInteger(parsed) && parsed > 0 && parsed < 65536) {
    return parsed;
  }
  return DEFAULT_LOCAL_PUSH_PORT;
}

function isLoopbackAddress(address: string | undefined) {
  return !address
    || address === "127.0.0.1"
    || address === "::1"
    || address === "::ffff:127.0.0.1";
}

function sendJson(res: ServerResponse, statusCode: number, payload: unknown) {
  const body = JSON.stringify(payload);
  res.writeHead(statusCode, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body).toString(),
  });
  res.end(body);
}

function readJsonBody(req: IncomingMessage) {
  return new Promise<PushVoucherPayload>((resolve, reject) => {
    const chunks: Buffer[] = [];
    let totalBytes = 0;

    req.on("data", (chunk) => {
      const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      totalBytes += buffer.length;
      if (totalBytes > MAX_REQUEST_BYTES) {
        reject(new Error("Request body is too large"));
        req.destroy();
        return;
      }
      chunks.push(buffer);
    });

    req.on("error", reject);
    req.on("end", () => {
      try {
        const raw = Buffer.concat(chunks).toString("utf8").trim();
        if (!raw) {
          throw new Error("Request body is empty");
        }
        const parsed = JSON.parse(raw);
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
          throw new Error("Voucher payload must be a JSON object");
        }
        resolve(parsed as PushVoucherPayload);
      } catch (error) {
        reject(error);
      }
    });
  });
}

function pickCompanyName(payload: PushVoucherPayload) {
  const explicitCompany = [
    payload.company_name,
    payload.company,
  ].find((value) => typeof value === "string" && value.trim());
  if (typeof explicitCompany === "string" && explicitCompany.trim()) {
    return explicitCompany.trim();
  }

  const enabledCompanies = store.get("companies").filter((company) => company.enabled);
  if (enabledCompanies.length === 1) {
    return enabledCompanies[0].name;
  }
  if (!enabledCompanies.length) {
    throw new Error("No enabled Tally companies are configured in TallyBridge");
  }
  throw new Error(
    "Multiple enabled Tally companies are configured. Include company_name in the payload.",
  );
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

function parseLastJsonObject(stdout: string) {
  const candidate = stdout
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .reverse()
    .find((line) => line.startsWith("{"));

  if (!candidate) {
    throw new Error("Python push worker did not return JSON output");
  }

  return JSON.parse(candidate);
}

export class LocalPushServer {
  private server: Server | null = null;
  private readonly port = resolveLocalPushPort();
  // In-flight push worker processes, killed on stop()/quit so they aren't
  // orphaned (a leaked engine.exe holds RAM and contends with Tally on :9000).
  private readonly activeChildren = new Set<ChildProcess>();

  constructor(
    private readonly mainWindow: BrowserWindow,
    private readonly syncEngine?: SyncEngine,
  ) {}

  start() {
    if (this.server) {
      return;
    }

    this.server = createServer((req, res) => {
      void this.handleRequest(req, res);
    });

    this.server.on("error", (error) => {
      const message = error instanceof Error ? error.message : String(error);
      this.log(`[Push API] Local server error: ${message}`);
      if ((error as NodeJS.ErrnoException).code === "EADDRINUSE") {
        dialog.showErrorBox(
          "TallyBridge Port Conflict",
          `Port ${this.port} is already in use.\n\nAnother TallyBridge instance may be running. Close it and restart the app.`,
        );
      }
    });

    this.server.listen(this.port, DEFAULT_LOCAL_PUSH_HOST, () => {
      this.log(
        `[Push API] Listening on http://${DEFAULT_LOCAL_PUSH_HOST}:${this.port}`,
      );
    });
  }

  stop() {
    for (const proc of this.activeChildren) {
      killProcessTree(proc);
    }
    this.activeChildren.clear();
    if (!this.server) {
      return;
    }
    this.server.close();
    this.server = null;
  }

  private async handleRequest(req: IncomingMessage, res: ServerResponse) {
    if (!isLoopbackAddress(req.socket.remoteAddress)) {
      sendJson(res, 403, { ok: false, error: "Local push API only accepts loopback requests" });
      return;
    }

    const requestUrl = new URL(req.url || "/", `http://${DEFAULT_LOCAL_PUSH_HOST}:${this.port}`);

    if (req.method === "GET" && requestUrl.pathname === "/health") {
      sendJson(res, 200, {
        ok: true,
        service: "TallyBridge Local Push API",
        host: DEFAULT_LOCAL_PUSH_HOST,
        port: this.port,
      });
      return;
    }

    if (req.method !== "POST" || requestUrl.pathname !== "/push-voucher") {
      sendJson(res, 404, { ok: false, error: "Not found" });
      return;
    }

    // A push writes vouchers into Tally. If ANYONE else is touching the
    // single-threaded gateway — a sync, the push-queue poller's own worker, or a
    // main-process request — stacking a write on top crashes TallyPrime
    // (c0000005). Defer with a retriable status; the voucher stays in the backend
    // queue and the poller redelivers it once Tally is free. (Fast-path check;
    // the race-free guard is inside runPythonPushWorker right before spawn.)
    if (this.syncEngine?.isSyncInProgress() || tallyGate.isBusy()) {
      this.log("[Push API] Tally is busy (sync or another push) — deferring; it will be retried from the queue.");
      sendJson(res, 503, {
        ok: false,
        deferred: true,
        error: "TallyBridge is busy with Tally; push deferred and will be retried.",
      });
      return;
    }

    try {
      const payload = await readJsonBody(req);
      const companyName = pickCompanyName(payload);
      const result = await this.runPythonPushWorker({
        ...payload,
        company_name: companyName,
      });
      // A deferred result (Tally became busy between the fast-path check and the
      // spawn) maps to 503 so the backend retries it, not 422 (a real failure).
      const statusCode = result.deferred ? 503 : result.ok ? 200 : 422;
      sendJson(res, statusCode, result);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unknown local push error";
      this.log(`[Push API] ${message}`);
      sendJson(res, 400, {
        ok: false,
        error: message,
      });
    }
  }

  private runPythonPushWorker(payload: PushVoucherPayload) {
    return new Promise<Record<string, unknown>>((resolve, reject) => {
      // Race-free gate: the handler's fast-path check ran before an await
      // (readJsonBody), during which the 5s poller could have started a worker.
      // This synchronous check right before beginPythonWork() closes that window
      // so two push workers never write to single-threaded Tally at once.
      if (tallyGate.isBusy()) {
        resolve({ ok: false, deferred: true, error: "Tally became busy; push deferred and will be retried." });
        return;
      }

      const scriptPath = isDev
        ? path.join(__dirname, "../../src/python/sync_main.py")
        : path.join(process.resourcesPath, "python", "sync_main.py");
      const pythonCommand = resolvePythonCommand(scriptPath);
      const env = {
        ...process.env,
        PYTHONUNBUFFERED: "1",
        TB_COMMAND: "push_voucher",
        TALLY_URL: store.get("tallyUrl"),
        TALLY_COMPANY: String(payload.company_name || "").trim(),
        TB_USER_DATA_DIR: app.getPath("userData"),
      };

      // Mark the gateway busy for the worker's lifetime so the status-bar probe
      // (and any other main-process Tally request) stays out of its way.
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

      let settled = false;

      const cleanup = () => {
        if (watchdog) {
          clearTimeout(watchdog);
          watchdog = null;
        }
        this.activeChildren.delete(proc);
        endPythonWork();
      };

      // Settle the worker Promise exactly once (idempotent), running cleanup
      // first. Used by the close/error handlers AND the watchdog fallback so a
      // worker whose 'close' never fires can't hang the request forever.
      const settle = (act: () => void) => {
        if (settled) {
          return;
        }
        settled = true;
        cleanup();
        act();
      };

      // Watchdog: kill a worker stuck writing to a locked Tally so it can't pin
      // the process / tally gate forever, log it (so the kill is diagnosable on
      // an unattended box), and settle after a grace period if 'close' never fires.
      const watchdogMs = resolvePushWorkerTimeoutMs();
      let watchdog: NodeJS.Timeout | null = setTimeout(() => {
        watchdog = null;
        this.log(
          `[Push API] Worker exceeded ${Math.round(watchdogMs / 1000)}s (Tally may be busy or locked) — terminating.`,
        );
        killProcessTree(proc);
        setTimeout(
          () => settle(() => reject(new Error("Push worker timed out — Tally may be busy or locked."))),
          2000,
        );
      }, watchdogMs);

      let stdout = "";
      let stderr = "";

      proc.stdout?.on("data", (chunk: Buffer) => {
        stdout += chunk.toString();
      });

      proc.stderr?.on("data", (chunk: Buffer) => {
        stderr += chunk.toString();
      });

      proc.on("error", (error) => {
        settle(() => reject(error));
      });

      proc.on("close", () => {
        settle(() => {
          try {
            const parsed = parseLastJsonObject(stdout);
            if (stderr.trim()) {
              parsed.stderr = stderr.trim();
            }
            resolve(parsed);
          } catch (error) {
            reject(
              new Error(
                stderr.trim()
                  || (error instanceof Error ? error.message : "Python push worker failed"),
              ),
            );
          }
        });
      });

      // Guard against EPIPE if the worker already died before we finish writing
      // its stdin — an unhandled 'error' here would crash the main process.
      proc.stdin?.on("error", () => {});
      proc.stdin?.write(JSON.stringify(payload));
      proc.stdin?.end();
    });
  }

  private log(line: string) {
    console.log(line);
    shipSyncLog({ company: "Push API", line });
    if (!this.mainWindow.isDestroyed()) {
      this.mainWindow.webContents.send("sync-log", {
        company: "Push API",
        line,
      });
    }
  }
}

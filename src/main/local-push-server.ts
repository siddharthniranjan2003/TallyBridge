import { spawn } from "child_process";
import { app, BrowserWindow, dialog } from "electron";
import { createServer, IncomingMessage, Server, ServerResponse } from "http";
import path from "path";
import isDev from "electron-is-dev";

import { store } from "./store";

const DEFAULT_LOCAL_PUSH_HOST = "127.0.0.1";
const DEFAULT_LOCAL_PUSH_PORT = 3002;
const MAX_REQUEST_BYTES = 1024 * 1024;

type PushVoucherPayload = Record<string, unknown> & {
  company_name?: unknown;
  company?: unknown;
};
type PushVoucherBatch = PushVoucherPayload[];

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

function isPushVoucherPayload(value: unknown): value is PushVoucherPayload {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function readJsonBody(req: IncomingMessage) {
  return new Promise<PushVoucherPayload | PushVoucherBatch>((resolve, reject) => {
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
        if (Array.isArray(parsed)) {
          if (!parsed.length) {
            throw new Error("Voucher payload array must not be empty");
          }
          if (!parsed.every(isPushVoucherPayload)) {
            throw new Error("Voucher payload array must contain only JSON objects");
          }
          resolve(parsed as PushVoucherBatch);
          return;
        }
        if (!isPushVoucherPayload(parsed)) {
          throw new Error(
            "Voucher payload must be a JSON object or non-empty array of voucher objects",
          );
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

function normalizeVoucherBatch(
  input: PushVoucherPayload | PushVoucherBatch,
): PushVoucherBatch {
  const vouchers = Array.isArray(input) ? input : [input];
  return vouchers.map((voucher) => ({
    ...voucher,
    company_name: pickCompanyName(voucher),
  }));
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

  constructor(private readonly mainWindow: BrowserWindow) {}

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

    try {
      const vouchers = normalizeVoucherBatch(await readJsonBody(req));
      const result = await this.runPythonPushWorker(vouchers);
      sendJson(res, result.ok ? 200 : 422, result);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unknown local push error";
      this.log(`[Push API] ${message}`);
      sendJson(res, 400, {
        ok: false,
        error: message,
      });
    }
  }

  private runPythonPushWorker(vouchers: PushVoucherBatch) {
    return new Promise<Record<string, unknown>>((resolve, reject) => {
      const scriptPath = isDev
        ? path.join(__dirname, "../../src/python/sync_main.py")
        : path.join(process.resourcesPath, "python", "sync_main.py");
      const groupedVouchers = new Map<string, PushVoucherBatch>();
      for (const voucher of vouchers) {
        const companyName = String(voucher.company_name || "").trim();
        if (!companyName) {
          reject(new Error("company_name is required for direct push mode"));
          return;
        }

        const existingBatch = groupedVouchers.get(companyName) || [];
        existingBatch.push(voucher);
        groupedVouchers.set(companyName, existingBatch);
      }

      const runSingleBatch = (companyName: string, batch: PushVoucherBatch) =>
        new Promise<Record<string, unknown>>((batchResolve, batchReject) => {
          const pythonCommand = resolvePythonCommand(scriptPath);
          const env = {
            ...process.env,
            PYTHONUNBUFFERED: "1",
            TB_COMMAND: "push_voucher",
            TALLY_URL: store.get("tallyUrl"),
            TALLY_COMPANY: companyName,
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
            batchReject(error);
          });

          proc.on("close", () => {
            try {
              const parsed = parseLastJsonObject(stdout);
              if (stderr.trim()) {
                parsed.stderr = stderr.trim();
              }
              batchResolve(parsed);
            } catch (error) {
              batchReject(
                new Error(
                  stderr.trim()
                    || (error instanceof Error ? error.message : "Python push worker failed"),
                ),
              );
            }
          });

          proc.stdin?.write(JSON.stringify(batch.length === 1 ? batch[0] : batch));
          proc.stdin?.end();
        });

      (async () => {
        const batchResults: Array<Record<string, unknown>> = [];
        let created = 0;
        let altered = 0;
        let errors = 0;
        let exceptions = 0;
        const lineErrors: string[] = [];
        let stderrCombined = "";

        for (const [companyName, batch] of groupedVouchers.entries()) {
          const batchResult = await runSingleBatch(companyName, batch);
          batchResults.push({
            company_name: companyName,
            voucher_count: batch.length,
            ...batchResult,
          });
          created += Number(batchResult.created || 0);
          altered += Number(batchResult.altered || 0);
          errors += Number(batchResult.errors || 0);
          exceptions += Number(batchResult.exceptions || 0);
          if (Array.isArray(batchResult.line_errors)) {
            lineErrors.push(
              ...batchResult.line_errors.map((entry) =>
                `[${companyName}] ${String(entry)}`),
            );
          }
          if (typeof batchResult.stderr === "string" && batchResult.stderr.trim()) {
            stderrCombined = stderrCombined
              ? `${stderrCombined}\n${batchResult.stderr.trim()}`
              : batchResult.stderr.trim();
          }
        }

        const ok = errors === 0 && (created > 0 || altered > 0);
        resolve({
          ok,
          voucher_count: vouchers.length,
          company_count: groupedVouchers.size,
          created,
          altered,
          errors,
          exceptions,
          line_errors: lineErrors,
          results: batchResults,
          ...(stderrCombined ? { stderr: stderrCombined } : {}),
        });
      })().catch(reject);
    });
  }

  private log(line: string) {
    console.log(line);
    if (!this.mainWindow.isDestroyed()) {
      this.mainWindow.webContents.send("sync-log", {
        company: "Push API",
        line,
      });
    }
  }
}

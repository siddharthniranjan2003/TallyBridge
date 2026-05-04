import { spawn } from "child_process";
import net from "net";
import { app, BrowserWindow } from "electron";
import path from "path";
import isDev from "electron-is-dev";
import {
  Company,
  normalizeSyncContractVersion,
  normalizeSyncIngestMode,
  resolveControlPlaneApiKey,
  resolveControlPlaneUrl,
  resolveSyncIngestKey,
  resolveSyncIngestUrl,
  store,
  SyncRecordCounts,
  updateCompanyStatus,
} from "./store";

type SyncLifecycleCallbacks = {
  onSyncStart?: () => void;
  onSyncComplete?: (hadErrors: boolean) => void;
  onCompanyError?: () => void;
};

export class SyncEngine {
  private timer: NodeJS.Timeout | null = null;
  private mainWindow: BrowserWindow;
  private isSyncing = false;
  private hadCompanyError = false;
  private lifecycleCallbacks: SyncLifecycleCallbacks = {};
  private paused = false;
  private currentProc: ReturnType<typeof spawn> | null = null;

  constructor(window: BrowserWindow) {
    this.mainWindow = window;
    this.paused = store.get("syncPaused") ?? false;
  }

  start() {
    console.log("[SyncEngine] Starting — polling for TallyPrime on port 9000...");
    if (this.paused) {
      this.emit("sync-log", { company: "System", line: "[TallyBridge] Sync is paused. Click Resume Sync to start." });
      return;
    }
    this.waitForTallyThenSync();
  }

  private waitForTallyThenSync() {
    if (this.paused) return;
    void this.checkTallyPort().then((up) => {
      if (this.paused) return;
      if (up) {
        console.log("[SyncEngine] TallyPrime reachable — beginning startup sync.");
        void this.runAllCompanies("startup");
      } else {
        console.log("[SyncEngine] TallyPrime not reachable yet, retrying in 60s...");
        this.emit("sync-log", {
          company: "System",
          line: "[TallyBridge] Waiting for TallyPrime to start (checking port 9000 every 60s)...",
        });
        this.timer = setTimeout(() => this.waitForTallyThenSync(), 60_000);
      }
    });
  }

  private checkTallyPort(): Promise<boolean> {
    const tallyUrl = store.get("tallyUrl") || "http://localhost:9000";
    let host = "127.0.0.1";
    let port = 9000;
    try {
      const parsed = new URL(tallyUrl);
      host = parsed.hostname || "127.0.0.1";
      port = Number(parsed.port) || (parsed.protocol === "https:" ? 443 : 80);
    } catch {
      // use defaults
    }
    return new Promise((resolve) => {
      const socket = new net.Socket();
      const done = (result: boolean) => { socket.destroy(); resolve(result); };
      socket.setTimeout(3000);
      socket.once("connect", () => done(true));
      socket.once("timeout", () => done(false));
      socket.once("error", () => done(false));
      socket.connect(port, host);
    });
  }

  stop() {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  pause() {
    this.paused = true;
    store.set("syncPaused", true);
    this.stop();
    if (this.currentProc?.pid) {
      if (process.platform === "win32") {
        spawn("taskkill", ["/pid", String(this.currentProc.pid), "/f", "/t"]);
      } else {
        this.currentProc.kill("SIGKILL");
      }
      this.currentProc = null;
    }
    this.isSyncing = false;
    this.emit("sync-complete", { at: new Date().toISOString() });
    this.emit("sync-paused", { paused: true });
  }

  resume() {
    this.paused = false;
    store.set("syncPaused", false);
    this.emit("sync-paused", { paused: false });
    this.scheduleNext(0);
  }

  isPaused() {
    return this.paused;
  }

  reschedule() {
    this.stop();
    this.scheduleNext();
  }

  setLifecycleCallbacks(callbacks: SyncLifecycleCallbacks) {
    this.lifecycleCallbacks = callbacks;
  }

  isSyncInProgress() {
    return this.isSyncing;
  }

  async syncNow() {
    if (this.paused) {
      this.emit("sync-log", { company: "System", line: "Sync is paused. Resume sync first." });
      return;
    }
    if (this.isSyncing) {
      this.emit("sync-log", { company: "System", line: "Sync already in progress..." });
      return;
    }
    this.stop();
    await this.runAllCompanies("manual");
  }

  private scheduleNext(delayMs?: number) {
    if (this.paused) return;
    const minutes = store.get("syncIntervalMinutes", 5);
    if (this.timer) {
      clearTimeout(this.timer);
    }
    const intervalMs = delayMs ?? minutes * 60 * 1000;
    this.timer = setTimeout(
      () => {
        this.timer = null;
        void this.runAllCompanies("heartbeat");
      },
      intervalMs
    );
    console.log(`[SyncEngine] Scheduled every ${minutes} minutes`);
  }

  private async runAllCompanies(trigger: "startup" | "manual" | "heartbeat") {
    if (this.paused) return;
    if (this.isSyncing) {
      this.emit("sync-log", { company: "System", line: "Sync already in progress..." });
      return;
    }

    const companies = store.get("companies").filter((c) => c.enabled);
    if (!companies.length) {
      this.emit("sync-log", { company: "System", line: "No companies to sync. Add a company first." });
      return;
    }

    // Pre-flight: confirm TallyPrime's port is open before spawning any Python process
    const tallyUp = await this.checkTallyPort();
    if (!tallyUp) {
      this.emit("sync-log", {
        company: "System",
        line: "[TallyBridge] TallyPrime not reachable — sync skipped. Will retry next interval.",
      });
      this.scheduleNext();
      return;
    }

    this.isSyncing = true;
    this.hadCompanyError = false;
    this.lifecycleCallbacks.onSyncStart?.();
    this.emit("sync-start", { companies: companies.map((c) => c.name) });

    try {
      for (const company of companies) {
        updateCompanyStatus(company.id, { lastSyncStatus: "syncing" });
        this.emit("company-status-change", {
          id: company.id,
          status: "syncing",
        });
        await this.syncOneCompany(company, trigger);
      }
    } finally {
      this.isSyncing = false;
      this.lifecycleCallbacks.onSyncComplete?.(this.hadCompanyError);
      this.emit("sync-complete", { at: new Date().toISOString() });
      // Refresh company list in UI
      this.emit("companies-updated", store.get("companies"));
      this.scheduleNext();
    }
  }

  private syncOneCompany(company: Company, trigger: "startup" | "manual" | "heartbeat"): Promise<void> {
    return new Promise((resolve) => {
      const config = store.store;
      const companyId = company.id;
      const companyName = company.name;

      // Python path — system Python in dev, bundled exe in production
      const pythonBin = isDev
        ? process.platform === "win32" ? "py" : "python3"
        : path.join(process.resourcesPath, "python-runtime", "tallybridge-engine.exe");

      const scriptPath = isDev
        ? path.join(__dirname, "../../src/python/sync_main.py")
        : path.join(process.resourcesPath, "python", "sync_main.py");

      const configuredReadMode = process.env.TB_READ_MODE || config.readMode || "auto";
      const odbcDsnOverride = config.odbcDsnOverride || process.env.TB_ODBC_DSN_OVERRIDE || "";
      const configuredSyncFromDate = (config.syncFromDate || process.env.TB_SYNC_FROM_DATE || "").trim();
      const configuredSyncToDate = (config.syncToDate || process.env.TB_SYNC_TO_DATE || "").trim();
      const controlPlaneUrl = resolveControlPlaneUrl(config);
      const controlPlaneApiKey = resolveControlPlaneApiKey(config);
      const syncIngestMode = normalizeSyncIngestMode(config.syncIngestMode);
      const syncIngestUrl = resolveSyncIngestUrl(config);
      const syncIngestKey = resolveSyncIngestKey(config);
      const syncContractVersion = normalizeSyncContractVersion(config.syncContractVersion);
      const backfillSignature = this.buildBackfillSignature(configuredSyncFromDate, configuredSyncToDate);
      const backfillPending = Boolean(
        backfillSignature
        && company.lastCompletedBackfillSignature !== backfillSignature
      );
      const shouldUseManualBackfill = backfillPending && trigger !== "heartbeat";
      const syncFromDate = shouldUseManualBackfill ? configuredSyncFromDate : "";
      const syncToDate = shouldUseManualBackfill ? configuredSyncToDate : "";
      const forceFullSync = !company.lastSyncedAt || shouldUseManualBackfill;

      this.emit("sync-log", {
        company: companyName,
        line: `[TallyBridge] Sync trigger: ${trigger}`,
      });
      this.emit("sync-log", {
        company: companyName,
        line: `[TallyBridge] Control plane: ${controlPlaneUrl || "not configured"} | Ingest mode: ${syncIngestMode}`,
      });
      if (syncIngestMode !== "render") {
        this.emit("sync-log", {
          company: companyName,
          line: `[TallyBridge] Direct ingest target: ${syncIngestUrl || "not configured"} | Contract v${syncContractVersion}`,
        });
      }
      if (shouldUseManualBackfill && backfillSignature) {
        this.emit("sync-log", {
          company: companyName,
          line: `[TallyBridge] One-time backfill armed for ${backfillSignature}.`,
        });
      } else if (backfillSignature && trigger === "heartbeat") {
        this.emit("sync-log", {
          company: companyName,
          line: "[TallyBridge] Heartbeat mode is ignoring the saved manual backfill range.",
        });
      }

      const env = {
        ...process.env,
        PYTHONUNBUFFERED: "1",
        TALLY_URL: config.tallyUrl,
        TALLY_COMPANY: companyName,
        TALLY_COMPANY_GUID: company.tallyGuid || "",
        TB_FORCE_FULL_SYNC: forceFullSync ? "1" : "",
        TB_READ_MODE: configuredReadMode,
        TB_ODBC_DSN_OVERRIDE: odbcDsnOverride,
        TB_SYNC_FROM_DATE: syncFromDate,
        TB_SYNC_TO_DATE: syncToDate,
        TB_SYNC_TRIGGER: trigger,
        TB_MANUAL_BACKFILL_PENDING: shouldUseManualBackfill ? "1" : "",
        BACKEND_URL: controlPlaneUrl,
        API_KEY: controlPlaneApiKey,
        CONTROL_PLANE_URL: controlPlaneUrl,
        CONTROL_PLANE_API_KEY: controlPlaneApiKey,
        SYNC_INGEST_MODE: syncIngestMode,
        SYNC_INGEST_URL: syncIngestUrl,
        SYNC_INGEST_KEY: syncIngestKey,
        SYNC_CONTRACT_VERSION: String(syncContractVersion),
        TB_USER_DATA_DIR: app.getPath("userData"),
      };

      const args = isDev
        ? process.platform === "win32" ? ["-3", "-u", scriptPath] : ["-u", scriptPath]
        : [];
      let outputLines: string[] = [];
      let errorOutput = "";
      let settled = false;
      let timeoutReason = "";
      const parsedIdleTimeoutMs = Number(process.env.TB_SYNC_PROCESS_IDLE_TIMEOUT_MS);
      const parsedHardTimeoutMs = Number(process.env.TB_SYNC_PROCESS_HARD_TIMEOUT_MS);
      const idleTimeoutMs = Number.isFinite(parsedIdleTimeoutMs) && parsedIdleTimeoutMs > 0
        ? parsedIdleTimeoutMs
        : 10 * 60 * 1000;
      const hardTimeoutMs = Number.isFinite(parsedHardTimeoutMs) && parsedHardTimeoutMs > 0
        ? parsedHardTimeoutMs
        : 45 * 60 * 1000;
      let idleInterval: NodeJS.Timeout | null = null;
      let hardTimeout: NodeJS.Timeout | null = null;
      let lastActivityAt = Date.now();

      const finalize = (code: number | null, overrideError?: string) => {
        if (settled) {
          return;
        }
        settled = true;
        if (idleInterval) {
          clearInterval(idleInterval);
        }
        if (hardTimeout) {
          clearTimeout(hardTimeout);
        }

        if (code === 0 && !overrideError) {
          const existing = store.get("companies").find((c) => c.id === companyId);
          let records = this.normalizeRecordCounts(existing?.lastSyncRecords);
          let status = "success";
          let syncMeta: any = undefined;
          try {
            // Find the last line that starts with "{" (tally/python logs might have trailing empty lines or junk)
            const validJsonLine = [...outputLines].reverse().find(line => line.trim().startsWith("{"));
            if (validJsonLine) {
              const parsed = JSON.parse(validJsonLine.trim());
              records = this.mergeRecordCounts(records, parsed.records);
              status = parsed.status || "success";
              syncMeta = parsed.sync_meta;
            }
          } catch (e) {
            console.error("JSON parse error from python output:", e);
          }

          // If sync was skipped (no changes), keep previous record counts
          if (status === "skipped") {
            records = this.normalizeRecordCounts(existing?.lastSyncRecords || records);
          }

          const update: Partial<Company> = {
            lastSyncStatus: "success",
            lastSyncedAt: new Date().toISOString(),
            lastSyncRecords: records,
            lastSyncError: undefined,
          };
          if (status === "success" && shouldUseManualBackfill && backfillSignature) {
            update.lastCompletedBackfillSignature = backfillSignature;
          }
          updateCompanyStatus(companyId, update);
          if (syncMeta?.change_detection_mode === "heartbeat") {
            this.emit("sync-log", {
              company: companyName,
              line: "[TallyBridge] Heartbeat completed its change check.",
            });
          }
          this.emit("company-synced", { id: companyId, name: companyName, records });
        } else {
          const errMsg = overrideError?.trim()
            || errorOutput.trim()
            || timeoutReason
            || "Unknown error (database insert failed or python crashed)";
          this.hadCompanyError = true;
          updateCompanyStatus(companyId, {
            lastSyncStatus: "error",
            lastSyncError: errMsg,
          });
          this.lifecycleCallbacks.onCompanyError?.();
          this.emit("company-error", { id: companyId, name: companyName, error: errMsg });
        }

        this.emit("companies-updated", store.get("companies"));
        resolve();
      };

      let proc: ReturnType<typeof spawn> | null = null;
      try {
        proc = spawn(pythonBin, args, { env });
        this.currentProc = proc;
      } catch (error) {
        finalize(1, error instanceof Error ? error.message : "Failed to start sync process");
        return;
      }

      const touchActivity = () => {
        lastActivityAt = Date.now();
      };

      idleInterval = setInterval(() => {
        if (settled) {
          return;
        }

        if (Date.now() - lastActivityAt > idleTimeoutMs) {
          timeoutReason = `Sync became idle for ${Math.round(idleTimeoutMs / 1000)}s and was stopped`;
          errorOutput = `${errorOutput}\n${timeoutReason}`.trim();
          this.emit("sync-log", {
            company: companyName,
            line: `[ERR] ${timeoutReason}`,
          });
          proc?.kill();
          setTimeout(() => finalize(1, timeoutReason), 1000);
        }
      }, 5000);

      hardTimeout = setTimeout(() => {
        timeoutReason = `Sync exceeded the hard limit of ${Math.round(hardTimeoutMs / 1000)}s`;
        errorOutput = `${errorOutput}\n${timeoutReason}`.trim();
        this.emit("sync-log", {
          company: companyName,
          line: `[ERR] ${timeoutReason}`,
        });
        proc?.kill();
        setTimeout(() => finalize(1, timeoutReason), 1000);
      }, hardTimeoutMs);

      proc.stdout?.on("data", (data: Buffer) => {
        touchActivity();
        const lines = data.toString().split("\n").filter(Boolean);
        lines.forEach((line) => {
          outputLines.push(line);
          this.emit("sync-log", { company: companyName, line });
        });
      });

      proc.stderr?.on("data", (data: Buffer) => {
        touchActivity();
        errorOutput += data.toString();
        this.emit("sync-log", {
          company: companyName,
          line: `[ERR] ${data.toString()}`,
        });
      });

      proc.on("error", (error: Error) => {
        const processError = `Sync process error: ${error.message}`;
        errorOutput = `${errorOutput}\n${processError}`.trim();
        this.emit("sync-log", {
          company: companyName,
          line: `[ERR] ${processError}`,
        });
        finalize(1, processError);
      });

      proc.on("close", (code) => {
        this.currentProc = null;
        finalize(code);
      });
    });
  }

  private emit(channel: string, data: any) {
    if (this.mainWindow && !this.mainWindow.isDestroyed()) {
      this.mainWindow.webContents.send(channel, data);
    }
  }

  private normalizeRecordCounts(
    value?: Partial<Record<keyof SyncRecordCounts, number>>,
  ): SyncRecordCounts {
    return {
      groups: typeof value?.groups === "number" ? value.groups : 0,
      ledgers: typeof value?.ledgers === "number" ? value.ledgers : 0,
      vouchers: typeof value?.vouchers === "number" ? value.vouchers : 0,
      stock: typeof value?.stock === "number" ? value.stock : 0,
      outstanding: typeof value?.outstanding === "number" ? value.outstanding : 0,
      profit_loss: typeof value?.profit_loss === "number" ? value.profit_loss : 0,
      balance_sheet: typeof value?.balance_sheet === "number" ? value.balance_sheet : 0,
      trial_balance: typeof value?.trial_balance === "number" ? value.trial_balance : 0,
    };
  }

  private mergeRecordCounts(
    base: SyncRecordCounts,
    updates: Partial<Record<keyof SyncRecordCounts, number>> | undefined,
  ): SyncRecordCounts {
    const normalizedBase = this.normalizeRecordCounts(base);
    return {
      groups: typeof updates?.groups === "number" ? updates.groups : normalizedBase.groups,
      ledgers: typeof updates?.ledgers === "number" ? updates.ledgers : normalizedBase.ledgers,
      vouchers: typeof updates?.vouchers === "number" ? updates.vouchers : normalizedBase.vouchers,
      stock: typeof updates?.stock === "number" ? updates.stock : normalizedBase.stock,
      outstanding: typeof updates?.outstanding === "number" ? updates.outstanding : normalizedBase.outstanding,
      profit_loss: typeof updates?.profit_loss === "number" ? updates.profit_loss : normalizedBase.profit_loss,
      balance_sheet: typeof updates?.balance_sheet === "number" ? updates.balance_sheet : normalizedBase.balance_sheet,
      trial_balance: typeof updates?.trial_balance === "number" ? updates.trial_balance : normalizedBase.trial_balance,
    };
  }

  private buildBackfillSignature(syncFromDate: string, syncToDate: string) {
    const fromValue = syncFromDate.trim();
    const toValue = syncToDate.trim();
    if (!fromValue && !toValue) {
      return "";
    }
    return `${fromValue || "open"}..${toValue || "open"}`;
  }
}

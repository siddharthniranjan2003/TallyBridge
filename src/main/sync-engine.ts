import { spawn } from "child_process";
import { BrowserWindow } from "electron";
import path from "path";
import isDev from "electron-is-dev";
import { store, updateCompanyStatus } from "./store";

export class SyncEngine {
  private timer: NodeJS.Timeout | null = null;
  private mainWindow: BrowserWindow;
  private isSyncing = false;

  constructor(window: BrowserWindow) {
    this.mainWindow = window;
  }

  start() {
    console.log("[SyncEngine] Starting...");
    // Run one sync immediately, then schedule
    setTimeout(() => this.runAllCompanies(), 3000);
    this.scheduleNext();
  }

  stop() {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  async syncNow() {
    if (this.isSyncing) {
      this.emit("sync-log", { company: "System", line: "Sync already in progress..." });
      return;
    }
    await this.runAllCompanies();
  }

  private scheduleNext() {
    const minutes = store.get("syncIntervalMinutes", 5);
    this.timer = setInterval(
      () => this.runAllCompanies(),
      minutes * 60 * 1000
    );
    console.log(`[SyncEngine] Scheduled every ${minutes} minutes`);
  }

  private async runAllCompanies() {
    const companies = store.get("companies").filter((c) => c.enabled);
    if (!companies.length) {
      this.emit("sync-log", { company: "System", line: "No companies to sync. Add a company first." });
      return;
    }

    this.isSyncing = true;
    this.emit("sync-start", { companies: companies.map((c) => c.name) });

    for (const company of companies) {
      updateCompanyStatus(company.id, { lastSyncStatus: "syncing" });
      this.emit("company-status-change", {
        id: company.id,
        status: "syncing",
      });
      await this.syncOneCompany(company.id, company.name);
    }

    this.isSyncing = false;
    this.emit("sync-complete", { at: new Date().toISOString() });
    // Refresh company list in UI
    this.emit("companies-updated", store.get("companies"));
  }

  private syncOneCompany(companyId: string, companyName: string): Promise<void> {
    return new Promise((resolve) => {
      const config = store.store;

      // Python path — system Python in dev, bundled exe in production
      const pythonBin = isDev
        ? "python"
        : path.join(process.resourcesPath, "python-runtime", "tallybridge-engine.exe");

      const scriptPath = isDev
        ? path.join(__dirname, "../../src/python/main.py")
        : path.join(process.resourcesPath, "python", "main.py");

      const env = {
        ...process.env,
        TALLY_URL: config.tallyUrl,
        TALLY_COMPANY: companyName,
        BACKEND_URL: config.backendUrl,
        API_KEY: config.apiKey,
      };

      const args = isDev ? [scriptPath] : [];
      const proc = spawn(pythonBin, args, { env });

      let outputLines: string[] = [];
      let errorOutput = "";

      proc.stdout?.on("data", (data: Buffer) => {
        const lines = data.toString().split("\n").filter(Boolean);
        lines.forEach((line) => {
          outputLines.push(line);
          this.emit("sync-log", { company: companyName, line });
        });
      });

      proc.stderr?.on("data", (data: Buffer) => {
        errorOutput += data.toString();
        this.emit("sync-log", {
          company: companyName,
          line: `[ERR] ${data.toString()}`,
        });
      });

      proc.on("close", (code) => {
        if (code === 0) {
          let records = { ledgers: 0, vouchers: 0, stock: 0, outstanding: 0 };
          let status = "success";
          try {
            // Find the last line that starts with "{" (tally/python logs might have trailing empty lines or junk)
            const validJsonLine = [...outputLines].reverse().find(line => line.trim().startsWith("{"));
            if (validJsonLine) {
              const parsed = JSON.parse(validJsonLine.trim());
              records = parsed.records || records;
              status = parsed.status || "success";
            }
          } catch (e) {
             console.error("JSON parse error from python output:", e);
          }

          // If sync was skipped (no changes), keep previous record counts
          if (status === "skipped") {
            const existing = store.get("companies").find((c) => c.id === companyId);
            records = existing?.lastSyncRecords || records;
          }

          updateCompanyStatus(companyId, {
            lastSyncStatus: "success",
            lastSyncedAt: new Date().toISOString(),
            lastSyncRecords: records,
            lastSyncError: undefined,
          });
          this.emit("company-synced", { id: companyId, name: companyName, records });
        } else {
          const errMsg = errorOutput.trim() || "Unknown error (database insert failed or python crashed)";
          updateCompanyStatus(companyId, {
            lastSyncStatus: "error",
            lastSyncError: errMsg,
          });
          this.emit("company-error", { id: companyId, name: companyName, error: errMsg });
        }

        this.emit("companies-updated", store.get("companies"));
        resolve();
      });
    });
  }

  private emit(channel: string, data: any) {
    if (this.mainWindow && !this.mainWindow.isDestroyed()) {
      this.mainWindow.webContents.send(channel, data);
    }
  }
}
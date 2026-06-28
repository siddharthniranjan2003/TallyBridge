import { contextBridge, ipcRenderer } from "electron";

const ALLOWED_CHANNELS = new Set([
  "sync-log",
  "sync-start",
  "sync-complete",
  "sync-paused",
  "config-updated",
  "company-status-change",
  "company-synced",
  "company-error",
  "companies-updated",
  "update-available",
  "update-progress",
  "update-downloaded",
  "update-error",
]);

contextBridge.exposeInMainWorld("electronAPI", {
  // Config
  getConfig: () => ipcRenderer.invoke("get-config"),
  saveSettings: (settings: any) => ipcRenderer.invoke("save-settings", settings),
  getAppVersion: () => ipcRenderer.invoke("get-app-version"),

  // Companies
  addCompany: (selection: { name: string; guid?: string; formalName?: string }) =>
    ipcRenderer.invoke("add-company", selection),
  removeCompany: (id: string) => ipcRenderer.invoke("remove-company", id),
  getCompanies: () => ipcRenderer.invoke("get-companies"),

  // Sync
  syncNow: () => ipcRenderer.invoke("sync-now"),
  pauseSync: () => ipcRenderer.invoke("pause-sync"),
  resumeSync: () => ipcRenderer.invoke("resume-sync"),
  checkTally: () => ipcRenderer.invoke("check-tally"),
  checkTallyCapabilities: () => ipcRenderer.invoke("check-tally-capabilities"),
  getTallyCompanies: () => ipcRenderer.invoke("get-tally-companies"),
  getTallyCompanyDateRanges: () => ipcRenderer.invoke("get-tally-company-date-ranges"),

  // Startup
  getStartupSetting: () => ipcRenderer.invoke("get-startup-setting"),
  setStartupSetting: (enable: boolean) => ipcRenderer.invoke("set-startup-setting", enable),

  // Logs
  openLogFile: () => ipcRenderer.invoke("open-log-file"),

  // App updates
  downloadUpdate: () => ipcRenderer.invoke("download-update"),
  installUpdate: () => ipcRenderer.invoke("install-update"),

  // Events from main → renderer
  on: (channel: string, callback: (...args: any[]) => void) => {
    if (!ALLOWED_CHANNELS.has(channel)) {
      return;
    }
    ipcRenderer.on(channel, callback);
  },
  off: (channel: string, callback: (...args: any[]) => void) => {
    if (!ALLOWED_CHANNELS.has(channel)) {
      return;
    }
    ipcRenderer.removeListener(channel, callback);
  },
});

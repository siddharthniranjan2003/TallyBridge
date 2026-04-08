import { contextBridge, ipcRenderer } from "electron";

const ALLOWED_CHANNELS = new Set([
  "sync-log",
  "sync-start",
  "sync-complete",
  "company-status-change",
  "company-synced",
  "company-error",
  "companies-updated",
]);

contextBridge.exposeInMainWorld("electronAPI", {
  // Config
  getConfig: () => ipcRenderer.invoke("get-config"),
  saveSettings: (settings: any) => ipcRenderer.invoke("save-settings", settings),

  // Companies
  addCompany: (selection: { name: string; guid?: string; formalName?: string }) =>
    ipcRenderer.invoke("add-company", selection),
  removeCompany: (id: string) => ipcRenderer.invoke("remove-company", id),
  getCompanies: () => ipcRenderer.invoke("get-companies"),

  // Sync
  syncNow: () => ipcRenderer.invoke("sync-now"),
  checkTally: () => ipcRenderer.invoke("check-tally"),
  checkTallyCapabilities: () => ipcRenderer.invoke("check-tally-capabilities"),
  getTallyCompanies: () => ipcRenderer.invoke("get-tally-companies"),
  getTallyCompanyDateRanges: () => ipcRenderer.invoke("get-tally-company-date-ranges"),

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

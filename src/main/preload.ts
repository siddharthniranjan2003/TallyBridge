import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("electronAPI", {
  // Config
  getConfig: () => ipcRenderer.invoke("get-config"),
  saveSettings: (settings: any) => ipcRenderer.invoke("save-settings", settings),

  // Companies
  addCompany: (name: string) => ipcRenderer.invoke("add-company", name),
  removeCompany: (id: string) => ipcRenderer.invoke("remove-company", id),
  getCompanies: () => ipcRenderer.invoke("get-companies"),

  // Sync
  syncNow: () => ipcRenderer.invoke("sync-now"),
  checkTally: () => ipcRenderer.invoke("check-tally"),
  getTallyCompanies: () => ipcRenderer.invoke("get-tally-companies"),

  // Events from main → renderer
  on: (channel: string, callback: (...args: any[]) => void) => {
    ipcRenderer.on(channel, callback);
  },
  off: (channel: string, callback: (...args: any[]) => void) => {
    ipcRenderer.removeListener(channel, callback);
  },
});
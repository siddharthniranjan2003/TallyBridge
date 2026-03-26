import { ipcMain, BrowserWindow } from "electron";
import axios from "axios";
import { store, addCompany, removeCompany } from "./store";
import { SyncEngine } from "./sync-engine";

export function setupIpcHandlers(engine: SyncEngine, window: BrowserWindow) {

  ipcMain.handle("get-config", () => store.store);

  ipcMain.handle("get-companies", () => store.get("companies"));

  ipcMain.handle("save-settings", (_, s) => {
    store.set("tallyUrl", s.tallyUrl);
    store.set("syncIntervalMinutes", Number(s.syncIntervalMinutes));
    store.set("backendUrl", s.backendUrl);
    store.set("apiKey", s.apiKey);
    store.set("accountEmail", s.accountEmail);
    return { success: true };
  });

  ipcMain.handle("add-company", async (_, name: string) => {
    const existing = store.get("companies").find(
      (c) => c.name.toLowerCase() === name.toLowerCase()
    );
    if (existing) {
      return { success: false, error: "Company already added." };
    }
    // Verify Tally is reachable
    try {
      const tallyUrl = store.get("tallyUrl");
      const testXml = `<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
        <BODY><EXPORTDATA><REQUESTDESC>
        <REPORTNAME>List of Companies</REPORTNAME>
        </REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>`;
      await axios.post(tallyUrl, testXml, {
        headers: { "Content-Type": "text/xml" },
        timeout: 5000,
      });
      const company = addCompany(name);
      window.webContents.send("companies-updated", store.get("companies"));
      return { success: true, company };
    } catch {
      return {
        success: false,
        error: "Cannot connect to TallyPrime. Make sure it is open and HTTP server is enabled on port 9000.",
      };
    }
  });

  ipcMain.handle("remove-company", (_, id: string) => {
    removeCompany(id);
    window.webContents.send("companies-updated", store.get("companies"));
    return { success: true };
  });

  ipcMain.handle("sync-now", async () => {
    await engine.syncNow();
    return { success: true };
  });

  ipcMain.handle("check-tally", async () => {
    try {
      const tallyUrl = store.get("tallyUrl");
      await axios.post(tallyUrl, "<test/>", {
        headers: { "Content-Type": "text/xml" },
        timeout: 3000,
      });
      return { connected: true };
    } catch {
      return { connected: false };
    }
  });
}
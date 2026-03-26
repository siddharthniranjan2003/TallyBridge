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
    const existing = store
      .get("companies")
      .find((c) => c.name.toLowerCase() === name.toLowerCase());
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
        error:
          "Cannot connect to TallyPrime. Make sure it is open and HTTP server is enabled on port 9000.",
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
      const testXml = `<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
      <BODY><EXPORTDATA><REQUESTDESC>
      <REPORTNAME>List of Companies</REPORTNAME>
      </REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>`;
      await axios.post(tallyUrl, Buffer.from(testXml, "utf16le"), {
        headers: {
          "Content-Type": "text/xml;charset=utf-16",
          "Content-Length": Buffer.byteLength(
            Buffer.from(testXml, "utf16le"),
          ).toString(),
        },
        timeout: 5000,
      });
      return { connected: true };
    } catch {
      return { connected: false };
    }
  });
  ipcMain.handle("get-tally-companies", async () => {
    try {
      const tallyUrl = store.get("tallyUrl");
      const xml = `<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>Collection of Ledgers</ID></HEADER><BODY><DESC><STATICVARIABLES><SVFROMDATE TYPE="Date">01-Jan-1970</SVFROMDATE><SVTODATE TYPE="Date">01-Jan-1970</SVTODATE><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES><TDL><TDLMESSAGE><COLLECTION NAME="Collection of Ledgers" ISMODIFY="No"><TYPE>Company</TYPE><FETCH>NAME,GUID,BASICCOMPANYFORMALNAME</FETCH><FILTERS>GroupFilter</FILTERS></COLLECTION><SYSTEM TYPE="FORMULAE" NAME="GroupFilter">$isaggregate = "No"</SYSTEM></TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>`;

      const xmlBuf = Buffer.from(xml, "utf16le");
      const response = await axios.post(tallyUrl, xmlBuf, {
        headers: {
          "Content-Type": "text/xml;charset=utf-16",
          "Content-Length": xmlBuf.length.toString(),
        },
        timeout: 5000,
        responseType: "arraybuffer",
      });

      // Decode UTF-16 response
      const decoded = Buffer.from(response.data).toString("utf16le");
      console.log("TALLY DECODED:", decoded);

      // Extract company names from NAME attribute or NAME tag
      const companies: string[] = [];

      // Try NAME attribute: <COMPANY NAME="Demo Trading Co">
      const attrMatches = decoded.matchAll(/<COMPANY[^>]+NAME="([^"]+)"/gi);
      for (const match of attrMatches) {
        const name = match[1]?.trim();
        if (name) companies.push(name);
      }

      // Fallback: <NAME TYPE="String">Demo Trading Co</NAME>
      if (companies.length === 0) {
        const tagMatches = decoded.matchAll(/<NAME[^>]*>([^<]+)<\/NAME>/gi);
        for (const match of tagMatches) {
          const name = match[1]?.trim();
          if (name) companies.push(name);
        }
      }

      return { success: true, companies };
    } catch (e) {
      console.error("get-tally-companies error:", e);
      return { success: false, companies: [] };
    }
  });
}

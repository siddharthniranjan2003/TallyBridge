import { ipcMain, BrowserWindow } from "electron";
import axios from "axios";
import { store, addCompany, removeCompany } from "./store";
import { SyncEngine } from "./sync-engine";

const TALLY_REQUEST_TIMEOUT_MS = 5000;

function decodeTallyResponse(data: Buffer, contentType = "") {
  const looksUtf16 = contentType.toLowerCase().includes("utf-16")
    || data.subarray(0, 2).equals(Buffer.from([0xff, 0xfe]))
    || data.subarray(0, 2).equals(Buffer.from([0xfe, 0xff]))
    || data.subarray(0, 64).includes(Buffer.from([0x00, 0x3c]))
    || data.subarray(0, 64).includes(Buffer.from([0x3c, 0x00]));

  if (looksUtf16) {
    try {
      const decoded = data.toString("utf16le");
      if (decoded.toUpperCase().includes("<ENVELOPE") || decoded.toUpperCase().includes("<RESPONSE")) {
        return decoded;
      }
    } catch {
      // Fall through to UTF-8 below.
    }
  }

  return data.toString("utf8");
}

async function postTallyXml(tallyUrl: string, xml: string, responseType: "text" | "arraybuffer" = "text") {
  const xmlBuf = Buffer.from(xml, "utf8");
  return axios.post(tallyUrl, xmlBuf, {
    headers: {
      "Content-Type": "text/xml;charset=utf-8",
      "Content-Length": xmlBuf.length.toString(),
    },
    timeout: TALLY_REQUEST_TIMEOUT_MS,
    responseType,
    transformResponse: response => response,
  });
}

export function setupIpcHandlers(engine: SyncEngine, window: BrowserWindow) {
  ipcMain.handle("get-config", () => store.store);

  ipcMain.handle("get-companies", () => store.get("companies"));

  ipcMain.handle("save-settings", (_, s) => {
    store.set("tallyUrl", s.tallyUrl);
    store.set("syncIntervalMinutes", Number(s.syncIntervalMinutes));
    store.set("backendUrl", s.backendUrl);
    store.set("apiKey", s.apiKey);
    store.set("accountEmail", s.accountEmail);
    engine.reschedule();
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
      await postTallyXml(tallyUrl, testXml);
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
      await postTallyXml(tallyUrl, testXml);
      return { connected: true };
    } catch {
      return { connected: false };
    }
  });
  ipcMain.handle("get-tally-companies", async () => {
    try {
      const tallyUrl = store.get("tallyUrl");
      const xml = `<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>CompanyList</ID></HEADER><BODY><DESC><STATICVARIABLES><SVFROMDATE TYPE="Date">01-Jan-1970</SVFROMDATE><SVTODATE TYPE="Date">01-Jan-1970</SVTODATE><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES><TDL><TDLMESSAGE><COLLECTION NAME="CompanyList" ISMODIFY="No"><TYPE>Company</TYPE><FETCH>NAME,GUID,BASICCOMPANYFORMALNAME</FETCH><FILTERS>GroupFilter</FILTERS></COLLECTION><SYSTEM TYPE="FORMULAE" NAME="GroupFilter">$isaggregate = "No"</SYSTEM></TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>`;

      const response = await postTallyXml(tallyUrl, xml, "arraybuffer");
      const decoded = decodeTallyResponse(
        Buffer.from(response.data),
        String(response.headers["content-type"] || ""),
      );
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

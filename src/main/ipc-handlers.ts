import { ipcMain, BrowserWindow } from "electron";
import { spawn } from "child_process";
import axios from "axios";
import isDev from "electron-is-dev";
import path from "path";
import { store, addCompany, removeCompany, TallyCompanySelection } from "./store";
import { SyncEngine } from "./sync-engine";

const TALLY_REQUEST_TIMEOUT_MS = 5000;

type TallyCompanyOption = {
  name: string;
  guid?: string;
  formalName?: string;
};

type TallyCompanyDateRange = {
  name: string;
  guid?: string;
  booksFrom: string | null;
  booksTo: string | null;
  availableFromDates: string[];
};

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

function decodeXmlEntities(value: string) {
  return value
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, "\"")
    .replace(/&apos;/g, "'");
}

function parseTallyPort(tallyUrl: string) {
  try {
    const parsed = new URL(tallyUrl);
    return Number(parsed.port || (parsed.protocol === "https:" ? 443 : 80));
  } catch {
    return 9000;
  }
}

function normalizeOptionalIsoDate(value: unknown) {
  const raw = typeof value === "string" ? value.trim() : "";
  if (!raw) {
    return "";
  }

  if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) {
    return raw;
  }

  if (/^\d{8}$/.test(raw)) {
    return `${raw.slice(0, 4)}-${raw.slice(4, 6)}-${raw.slice(6, 8)}`;
  }

  return "";
}

function toIsoDate(year: number, month: number, day: number) {
  if (
    !Number.isInteger(year)
    || !Number.isInteger(month)
    || !Number.isInteger(day)
    || month < 1
    || month > 12
    || day < 1
    || day > 31
  ) {
    return null;
  }

  const probe = new Date(Date.UTC(year, month - 1, day));
  if (
    probe.getUTCFullYear() !== year
    || probe.getUTCMonth() !== month - 1
    || probe.getUTCDate() !== day
  ) {
    return null;
  }

  return `${String(year).padStart(4, "0")}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

function parseTallyDateToIso(value: string | undefined) {
  const raw = value ? decodeXmlEntities(value).trim() : "";
  if (!raw) {
    return null;
  }

  const compactMatch = /^(\d{4})(\d{2})(\d{2})$/.exec(raw);
  if (compactMatch) {
    const iso = toIsoDate(
      Number(compactMatch[1]),
      Number(compactMatch[2]),
      Number(compactMatch[3]),
    );
    return iso;
  }

  const monthLookup: Record<string, number> = {
    jan: 1,
    feb: 2,
    mar: 3,
    apr: 4,
    may: 5,
    jun: 6,
    jul: 7,
    aug: 8,
    sep: 9,
    oct: 10,
    nov: 11,
    dec: 12,
  };

  const alphaDateMatch = /^(\d{1,2})-([A-Za-z]{3})-(\d{2}|\d{4})$/.exec(raw);
  if (alphaDateMatch) {
    const day = Number(alphaDateMatch[1]);
    const month = monthLookup[alphaDateMatch[2].toLowerCase()];
    const yearRaw = alphaDateMatch[3];
    const year = yearRaw.length === 2
      ? (Number(yearRaw) >= 70 ? 1900 + Number(yearRaw) : 2000 + Number(yearRaw))
      : Number(yearRaw);
    if (!month) {
      return null;
    }
    return toIsoDate(year, month, day);
  }

  const numericDateMatch = /^(\d{1,2})-(\d{1,2})-(\d{4})$/.exec(raw);
  if (numericDateMatch) {
    return toIsoDate(
      Number(numericDateMatch[3]),
      Number(numericDateMatch[2]),
      Number(numericDateMatch[1]),
    );
  }

  return null;
}

function buildAvailableFromDates(booksFrom: string | null, booksTo: string | null) {
  if (!booksFrom) {
    return [];
  }

  const [startYearRaw, startMonthRaw, startDayRaw] = booksFrom.split("-");
  const startYear = Number(startYearRaw);
  const startMonth = Number(startMonthRaw);
  const startDay = Number(startDayRaw);
  if (!startYear || !startMonth || !startDay) {
    return [booksFrom];
  }

  const upperBound = booksTo || new Date().toISOString().slice(0, 10);
  const endYear = Number(upperBound.slice(0, 4));
  const options: string[] = [];
  for (let year = startYear; year <= endYear + 1; year += 1) {
    const candidate = toIsoDate(year, startMonth, startDay);
    if (!candidate) {
      continue;
    }
    if (candidate < booksFrom) {
      continue;
    }
    if (candidate > upperBound) {
      break;
    }
    options.push(candidate);
    if (options.length >= 200) {
      break;
    }
  }

  if (!options.length) {
    return [booksFrom];
  }

  return options;
}

async function probeOdbcCapabilities(tallyUrl: string, odbcDsnOverride = "") {
  if (process.platform !== "win32") {
    return {
      state: "not_configured",
      dsn: null,
      supported_sections: [],
      message: "ODBC helper is only available on Windows.",
    };
  }

  const helperPath = isDev
    ? path.join(__dirname, "../../src/python/tally_odbc_helper.ps1")
    : path.join(process.resourcesPath, "python", "tally_odbc_helper.ps1");
  const powerShellPath = path.join(
    process.env.SystemRoot || "C:\\Windows",
    "SysWOW64",
    "WindowsPowerShell",
    "v1.0",
    "powershell.exe",
  );

  return await new Promise<Record<string, unknown>>((resolve) => {
    const proc = spawn(
      powerShellPath,
      ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", helperPath],
      {
        stdio: ["pipe", "pipe", "pipe"],
      },
    );

    let stdout = "";
    let stderr = "";
    proc.stdout.on("data", (chunk: Buffer) => {
      stdout += chunk.toString("utf8");
    });
    proc.stderr.on("data", (chunk: Buffer) => {
      stderr += chunk.toString("utf8");
    });
    proc.on("error", (error) => {
      resolve({
        state: "error",
        dsn: null,
        supported_sections: [],
        message: error.message,
      });
    });
    proc.on("close", () => {
      const line = stdout
        .split(/\r?\n/)
        .map((value) => value.trim())
        .find(Boolean);
      if (!line) {
        resolve({
          state: "error",
          dsn: null,
          supported_sections: [],
          message: stderr.trim() || "ODBC helper did not return a response.",
        });
        return;
      }

      try {
        resolve(JSON.parse(line));
      } catch {
        resolve({
          state: "error",
          dsn: null,
          supported_sections: [],
          message: `Unexpected ODBC helper response: ${line}`,
        });
      }
    });

    proc.stdin.write(`${JSON.stringify({
      cmd: "probe",
      dsn_override: odbcDsnOverride,
      port: parseTallyPort(tallyUrl),
      sections: ["groups", "ledgers", "stock_items"],
      queries: {
        groups: "Select $Name, $Parent, $MasterID, $IsRevenue, $AffectsStock, $IsSubLedger from Group",
        ledgers: "Select $Name, $Parent, $OpeningBalance, $ClosingBalance, $MasterID from Ledger",
        stock_items: "Select $Name, $Parent, $BaseUnits, $ClosingBalance, $ClosingValue, $ClosingRate from StockItem",
      },
      timeout_seconds: 8,
    })}\n`);
    proc.stdin.end();
  });
}

function extractAttributeValue(input: string, attributeName: string) {
  const match = new RegExp(`${attributeName}="([^"]*)"`, "i").exec(input);
  return match?.[1] ? decodeXmlEntities(match[1].trim()) : undefined;
}

function extractTagValue(input: string, tagName: string) {
  const match = new RegExp(`<${tagName}[^>]*>([\\s\\S]*?)</${tagName}>`, "i").exec(input);
  return match?.[1] ? decodeXmlEntities(match[1].trim()) : undefined;
}

function getCompanyOptionKey(company: TallyCompanySelection) {
  return company.guid?.trim() || company.name.trim().toLowerCase();
}

function parseTallyCompanies(decodedXml: string): TallyCompanyOption[] {
  const companies: TallyCompanyOption[] = [];
  const seen = new Set<string>();
  const companyBlocks = decodedXml.matchAll(/<COMPANY\b([^>]*)>([\s\S]*?)<\/COMPANY>/gi);

  for (const match of companyBlocks) {
    const attributes = match[1] || "";
    const body = match[2] || "";
    const name = extractAttributeValue(attributes, "NAME") || extractTagValue(body, "NAME");
    const guid = extractTagValue(body, "GUID") || extractAttributeValue(attributes, "GUID");
    const formalName = extractTagValue(body, "BASICCOMPANYFORMALNAME") || extractTagValue(body, "FORMALNAME");

    if (!name) {
      continue;
    }

    const company: TallyCompanyOption = {
      name,
      guid: guid || undefined,
      formalName: formalName || undefined,
    };
    const key = getCompanyOptionKey(company);
    if (seen.has(key)) {
      continue;
    }

    seen.add(key);
    companies.push(company);
  }

  if (companies.length) {
    return companies;
  }

  const fallbackNames = decodedXml.matchAll(/<NAME[^>]*>([^<]+)<\/NAME>/gi);
  for (const match of fallbackNames) {
    const name = decodeXmlEntities(match[1]?.trim() || "");
    if (!name) {
      continue;
    }

    const company = { name };
    const key = getCompanyOptionKey(company);
    if (seen.has(key)) {
      continue;
    }

    seen.add(key);
    companies.push(company);
  }

  return companies;
}

function parseTallyCompanyDateRanges(decodedXml: string): TallyCompanyDateRange[] {
  const companies: TallyCompanyDateRange[] = [];
  const seen = new Set<string>();
  const companyBlocks = decodedXml.matchAll(/<COMPANY\b([^>]*)>([\s\S]*?)<\/COMPANY>/gi);

  for (const match of companyBlocks) {
    const attributes = match[1] || "";
    const body = match[2] || "";
    const name = extractAttributeValue(attributes, "NAME") || extractTagValue(body, "NAME");
    const guid = extractTagValue(body, "GUID") || extractAttributeValue(attributes, "GUID");
    const booksFromRaw = extractTagValue(body, "BOOKSFROM");
    const booksToRaw = extractTagValue(body, "BOOKSTO");
    const booksFrom = parseTallyDateToIso(booksFromRaw);
    const booksTo = parseTallyDateToIso(booksToRaw);

    if (!name) {
      continue;
    }

    const company: TallyCompanyDateRange = {
      name,
      guid: guid || undefined,
      booksFrom,
      booksTo,
      availableFromDates: buildAvailableFromDates(booksFrom, booksTo),
    };
    const key = getCompanyOptionKey(company);
    if (seen.has(key)) {
      continue;
    }

    seen.add(key);
    companies.push(company);
  }

  return companies;
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
    store.set("readMode", s.readMode || "auto");
    store.set("odbcDsnOverride", s.odbcDsnOverride || "");
    store.set("syncFromDate", normalizeOptionalIsoDate(s.syncFromDate));
    store.set("syncToDate", normalizeOptionalIsoDate(s.syncToDate));
    engine.reschedule();
    return { success: true };
  });

  ipcMain.handle("add-company", async (_, selection: TallyCompanySelection) => {
    const name = typeof selection?.name === "string" ? selection.name.trim() : "";
    const guid = typeof selection?.guid === "string" ? selection.guid.trim() : "";
    const formalName = typeof selection?.formalName === "string" ? selection.formalName.trim() : "";
    const normalizedName = name.toLowerCase();

    if (!name) {
      return { success: false, error: "Company name is required." };
    }

    const existing = store
      .get("companies")
      .find((c) => {
        if (guid && c.tallyGuid) {
          return c.tallyGuid === guid;
        }

        return c.name.trim().toLowerCase() === normalizedName && (!guid || !c.tallyGuid);
      });
    if (existing) {
      return { success: false, error: "This Tally company is already added." };
    }
    // Verify Tally is reachable
    try {
      const tallyUrl = store.get("tallyUrl");
      const testXml = `<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
        <BODY><EXPORTDATA><REQUESTDESC>
        <REPORTNAME>List of Companies</REPORTNAME>
        </REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>`;
      await postTallyXml(tallyUrl, testXml);
      const company = addCompany({
        name,
        guid: guid || undefined,
        formalName: formalName || undefined,
      });
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
  ipcMain.handle("check-tally-capabilities", async () => {
    const tallyUrl = store.get("tallyUrl");
    const readMode = store.get("readMode", "auto");
    const odbcDsnOverride = store.get("odbcDsnOverride", "");

    let xmlConnected = false;
    let xmlError: string | null = null;
    try {
      const testXml = `<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
      <BODY><EXPORTDATA><REQUESTDESC>
      <REPORTNAME>List of Companies</REPORTNAME>
      </REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>`;
      await postTallyXml(tallyUrl, testXml);
      xmlConnected = true;
    } catch (error: any) {
      xmlError = error?.message || "Could not connect to Tally XML";
    }

    const odbc = readMode === "xml-only"
      ? {
          state: "disabled",
          dsn: null,
          supported_sections: [],
          message: "Read mode is XML only.",
        }
      : await probeOdbcCapabilities(tallyUrl, odbcDsnOverride);

    return {
      xml: {
        connected: xmlConnected,
        error: xmlError,
      },
      odbc,
      readMode,
      transportPlan: {
        groups: odbc.state === "ok" ? "odbc-first" : "xml",
        ledgers: odbc.state === "ok" ? "odbc-first" : "xml",
        stock_items: odbc.state === "ok" ? "odbc-first" : "xml",
        vouchers: "xml",
        outstanding: "xml",
        profit_loss: "xml",
        balance_sheet: "xml",
        trial_balance: "xml",
      },
    };
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
      const companies = parseTallyCompanies(decoded);

      return { success: true, companies };
    } catch (e) {
      console.error("get-tally-companies error:", e);
      return { success: false, companies: [] };
    }
  });

  ipcMain.handle("get-tally-company-date-ranges", async () => {
    try {
      const tallyUrl = store.get("tallyUrl");
      const xml = `<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>CompanyDateRanges</ID></HEADER><BODY><DESC><STATICVARIABLES><SVFROMDATE TYPE="Date">01-Jan-1970</SVFROMDATE><SVTODATE TYPE="Date">01-Jan-1970</SVTODATE><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES><TDL><TDLMESSAGE><COLLECTION NAME="CompanyDateRanges" ISMODIFY="No"><TYPE>Company</TYPE><FETCH>NAME,GUID,BOOKSFROM,BOOKSTO</FETCH><FILTERS>GroupFilter</FILTERS></COLLECTION><SYSTEM TYPE="FORMULAE" NAME="GroupFilter">$isaggregate = "No"</SYSTEM></TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>`;

      const response = await postTallyXml(tallyUrl, xml, "arraybuffer");
      const decoded = decodeTallyResponse(
        Buffer.from(response.data),
        String(response.headers["content-type"] || ""),
      );
      const companies = parseTallyCompanyDateRanges(decoded);

      return { success: true, companies };
    } catch (e: any) {
      console.error("get-tally-company-date-ranges error:", e);
      return {
        success: false,
        companies: [],
        error: e?.message || "Could not read company date ranges from Tally.",
      };
    }
  });
}

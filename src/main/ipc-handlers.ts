import { ipcMain, BrowserWindow, app, shell } from "electron";
import { spawn } from "child_process";
import axios from "axios";
import isDev from "electron-is-dev";
import path from "path";
import { logger } from "./logger";
import {
  addCompany,
  normalizeSyncContractVersion,
  normalizeSyncIngestMode,
  removeCompany,
  resetStaleSyncStatuses,
  resolveControlPlaneApiKey,
  resolveControlPlaneUrl,
  resolveSyncIngestKey,
  resolveSyncIngestUrl,
  store,
  TallyCompanySelection,
} from "./store";
import { SyncEngine } from "./sync-engine";
import { tallyGate } from "./tally-gate";

const TALLY_REQUEST_TIMEOUT_MS = 5000;

// Last successful connectivity result, served to the status-bar poll while Tally
// is busy so we don't fire a competing probe at the single-threaded gateway.
let lastTallyConnected = true;

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

// Secrets we never hand to the renderer in cleartext. get-config replaces a set
// value with SECRET_SENTINEL; save-settings restores the stored value when it
// receives the sentinel back (i.e. the user didn't change the field).
const SECRET_SENTINEL = "__TB_SECRET_UNCHANGED__";
const SECRET_CONFIG_KEYS = ["apiKey", "controlPlaneApiKey", "syncIngestKey"] as const;

function parseTallyPort(tallyUrl: string) {
  try {
    const parsed = new URL(tallyUrl);
    // Fall back to Tally's port 9000 (not the protocol default 80/443) when the
    // URL omits an explicit port, so a no-port tallyUrl still probes Tally.
    return Number(parsed.port) || 9000;
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
        windowsHide: true,
      },
    );

    // Settle once, and ALWAYS bound the probe: a wedged ODBC handshake never
    // emits 'close', so without this the renderer await hangs forever and the
    // powershell child leaks holding a Tally ODBC connection. Kill the tree and
    // resolve with an error if it overruns.
    let settled = false;
    const done = (result: Record<string, unknown>) => {
      if (settled) return;
      settled = true;
      clearTimeout(watchdog);
      resolve(result);
    };
    const watchdog = setTimeout(() => {
      if (proc.pid) {
        try {
          spawn("taskkill", ["/pid", String(proc.pid), "/f", "/t"], { windowsHide: true });
        } catch {
          try { proc.kill(); } catch { /* best effort */ }
        }
      }
      done({
        state: "error",
        dsn: null,
        supported_sections: [],
        message: "ODBC probe timed out — TallyPrime may be busy or locked.",
      });
    }, 15000);

    let stdout = "";
    let stderr = "";
    proc.stdout.on("data", (chunk: Buffer) => {
      stdout += chunk.toString("utf8");
    });
    proc.stderr.on("data", (chunk: Buffer) => {
      stderr += chunk.toString("utf8");
    });
    proc.on("error", (error) => {
      done({
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
        done({
          state: "error",
          dsn: null,
          supported_sections: [],
          message: stderr.trim() || "ODBC helper did not return a response.",
        });
        return;
      }

      try {
        done(JSON.parse(line));
      } catch {
        done({
          state: "error",
          dsn: null,
          supported_sections: [],
          message: `Unexpected ODBC helper response: ${line}`,
        });
      }
    });

    // Guard EPIPE if the child died before stdin is fully written.
    proc.stdin.on("error", () => {});
    proc.stdin.write(`${JSON.stringify({
      cmd: "probe",
      dsn_override: odbcDsnOverride,
      port: parseTallyPort(tallyUrl),
      sections: ["groups", "ledgers", "stock_items"],
      queries: {
        groups: "Select $Name, $Parent, $MasterID, $IsRevenue, $AffectsStock, $IsSubLedger from Groups",
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

function extractTallyLineErrors(decodedXml: string) {
  return Array.from(decodedXml.matchAll(/<LINEERROR[^>]*>([\s\S]*?)<\/LINEERROR>/gi))
    .map((match) => decodeXmlEntities(match[1]?.trim() || ""))
    .filter(Boolean);
}

function extractTallyStatusError(decodedXml: string) {
  const statusMatch = /<STATUS[^>]*>\s*0\s*<\/STATUS>/i.exec(decodedXml);
  if (!statusMatch) {
    return null;
  }

  const dataMatch = /<DATA[^>]*>([\s\S]*?)<\/DATA>/i.exec(decodedXml);
  if (!dataMatch?.[1]) {
    return "Tally returned an unknown error.";
  }

  const cleaned = decodeXmlEntities(dataMatch[1])
    .replace(/<[^>]+>/g, " ")
    .replace(/\s+/g, " ")
    .trim();

  return cleaned || "Tally returned an unknown error.";
}

function getTallyResponseError(decodedXml: string) {
  const lineErrors = extractTallyLineErrors(decodedXml);
  if (lineErrors.length) {
    return lineErrors.join(" | ");
  }

  return extractTallyStatusError(decodedXml);
}

function assertTallyXmlSuccess(decodedXml: string) {
  const error = getTallyResponseError(decodedXml);
  if (error) {
    throw new Error(error);
  }

  return decodedXml;
}

function buildTallyCompanyCollectionXml(id: string, fetchFields: string) {
  return `<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>${id}</ID></HEADER><BODY><DESC><STATICVARIABLES><SVFROMDATE TYPE="Date">01-Jan-1970</SVFROMDATE><SVTODATE TYPE="Date">01-Jan-1970</SVTODATE><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES><TDL><TDLMESSAGE><COLLECTION NAME="${id}" ISMODIFY="No"><TYPE>Company</TYPE><FETCH>${fetchFields}</FETCH><FILTERS>GroupFilter</FILTERS></COLLECTION><SYSTEM TYPE="FORMULAE" NAME="GroupFilter">$isaggregate = "No"</SYSTEM></TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>`;
}

async function postTallyXml(tallyUrl: string, xml: string, responseType: "text" | "arraybuffer" = "text") {
  const xmlBuf = Buffer.from(xml, "utf8");
  // Route every main-process Tally request through the shared gate so two
  // requests can never overlap on TallyPrime's single-threaded gateway.
  return tallyGate.runExclusive(() =>
    axios.post(tallyUrl, xmlBuf, {
      headers: {
        "Content-Type": "text/xml;charset=utf-8",
        "Content-Length": xmlBuf.length.toString(),
      },
      timeout: TALLY_REQUEST_TIMEOUT_MS,
      responseType,
      transformResponse: response => response,
    }),
  );
}

async function postAndDecodeTallyXml(tallyUrl: string, xml: string) {
  const response = await postTallyXml(tallyUrl, xml, "arraybuffer");
  return decodeTallyResponse(
    Buffer.from(response.data),
    String(response.headers["content-type"] || ""),
  );
}

async function fetchTallyCompanies(tallyUrl: string) {
  const decoded = await postAndDecodeTallyXml(
    tallyUrl,
    buildTallyCompanyCollectionXml("CompanyList", "NAME,GUID,BASICCOMPANYFORMALNAME"),
  );

  assertTallyXmlSuccess(decoded);
  return parseTallyCompanies(decoded);
}

async function fetchTallyCompanyDateRanges(tallyUrl: string) {
  const decoded = await postAndDecodeTallyXml(
    tallyUrl,
    buildTallyCompanyCollectionXml("CompanyDateRanges", "NAME,GUID,BOOKSFROM,BOOKSTO"),
  );

  assertTallyXmlSuccess(decoded);
  return parseTallyCompanyDateRanges(decoded);
}

function doesTallyCompanyMatchSelection(
  company: TallyCompanyOption,
  selection: TallyCompanySelection,
) {
  const selectionGuid = selection.guid?.trim();
  if (selectionGuid && company.guid?.trim()) {
    return company.guid.trim() === selectionGuid;
  }

  return company.name.trim().toLowerCase() === selection.name.trim().toLowerCase();
}

export function setupIpcHandlers(engine: SyncEngine, window: BrowserWindow) {
  if (resetStaleSyncStatuses()) {
    window.webContents.once("did-finish-load", () => {
      window.webContents.send("companies-updated", store.get("companies"));
    });
  }

  ipcMain.handle("get-config", () => {
    // Don't ship raw secrets to the renderer. Replace any set key with a
    // sentinel so the Settings form can show "configured" (masked) without
    // exposing the value; save-settings preserves the stored value when it
    // receives the sentinel back unchanged.
    const cfg: Record<string, unknown> = { ...store.store };
    for (const key of SECRET_CONFIG_KEYS) {
      if (typeof cfg[key] === "string" && cfg[key]) {
        cfg[key] = SECRET_SENTINEL;
      }
    }
    return cfg;
  });

  ipcMain.handle("get-app-version", () => app.getVersion());

  ipcMain.handle("get-companies", () => store.get("companies"));

  ipcMain.handle("save-settings", (_, s) => {
    // If the renderer sent back the masked sentinel (the user left a secret
    // field untouched), keep the currently-stored secret instead of wiping it.
    const unmaskSecret = (incoming: any, storeKey: string): any =>
      incoming === SECRET_SENTINEL ? store.get(storeKey as any, "") : incoming;
    const incomingApiKey = unmaskSecret(s.apiKey, "apiKey");
    const incomingControlKey = unmaskSecret(s.controlPlaneApiKey, "controlPlaneApiKey");
    const incomingIngestKey = unmaskSecret(s.syncIngestKey, "syncIngestKey");

    const legacyBackendUrl = typeof s.backendUrl === "string" ? s.backendUrl.trim() : "";
    const legacyApiKey = typeof incomingApiKey === "string" ? incomingApiKey.trim() : "";
    const controlPlaneUrl = resolveControlPlaneUrl({
      controlPlaneUrl: s.controlPlaneUrl,
      backendUrl: legacyBackendUrl,
    });
    const controlPlaneApiKey = resolveControlPlaneApiKey({
      controlPlaneApiKey: incomingControlKey,
      apiKey: legacyApiKey,
    });
    const syncIngestUrl = resolveSyncIngestUrl({
      syncIngestUrl: s.syncIngestUrl,
    });
    const syncIngestKey = resolveSyncIngestKey({
      syncIngestKey: incomingIngestKey,
    });

    // Validate the sync interval: a NaN / 0 / negative value would schedule
    // back-to-back full syncs that hammer single-threaded TallyPrime. Keep the
    // previous value if invalid; floor at 5 minutes.
    const rawInterval = Number(s.syncIntervalMinutes);
    const syncIntervalMinutes =
      Number.isFinite(rawInterval) && rawInterval >= 5
        ? Math.floor(rawInterval)
        : store.get("syncIntervalMinutes", 360);

    // Normalize the Tally URL: trim, default to the standard endpoint if blank,
    // and ensure a scheme so URL parsing + port detection work.
    let tallyUrl = typeof s.tallyUrl === "string" ? s.tallyUrl.trim() : "";
    const lowerUrl = tallyUrl.toLowerCase();
    if (!tallyUrl) {
      tallyUrl = "http://localhost:9000";
    } else if (!lowerUrl.startsWith("http://") && !lowerUrl.startsWith("https://")) {
      tallyUrl = `http://${tallyUrl}`;
    }

    store.set("tallyUrl", tallyUrl);
    store.set("syncIntervalMinutes", syncIntervalMinutes);
    store.set("backendUrl", controlPlaneUrl);
    store.set("apiKey", controlPlaneApiKey);
    store.set("controlPlaneUrl", controlPlaneUrl);
    store.set("controlPlaneApiKey", controlPlaneApiKey);
    store.set("syncIngestMode", normalizeSyncIngestMode(s.syncIngestMode));
    store.set("syncIngestUrl", syncIngestUrl);
    store.set("syncIngestKey", syncIngestKey);
    store.set("syncContractVersion", normalizeSyncContractVersion(s.syncContractVersion));
    store.set("accountEmail", s.accountEmail);
    store.set("readMode", s.readMode || "auto");
    store.set("odbcDsnOverride", s.odbcDsnOverride || "");
    store.set("syncFromDate", normalizeOptionalIsoDate(s.syncFromDate));
    store.set("syncToDate", normalizeOptionalIsoDate(s.syncToDate));
    engine.reschedule();
    window.webContents.send("config-updated", {
      syncIntervalMinutes: store.get("syncIntervalMinutes", 360),
      syncPaused: Boolean(store.get("syncPaused")),
    });
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
      const companies = await fetchTallyCompanies(tallyUrl);
      const companyStillExists = companies.some((company) => doesTallyCompanyMatchSelection(company, selection));
      if (!companyStillExists) {
        return {
          success: false,
          error: "The selected company is no longer available in TallyPrime. Refresh the company list and try again.",
        };
      }

      const company = addCompany({
        name,
        guid: guid || undefined,
        formalName: formalName || undefined,
      });
      window.webContents.send("companies-updated", store.get("companies"));
      return { success: true, company };
    } catch (error: any) {
      return {
        success: false,
        error:
          error?.message
          || "Cannot connect to TallyPrime. Make sure it is open and HTTP server is enabled on port 9000.",
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

  ipcMain.handle("pause-sync", () => {
    engine.pause();
    return { paused: true };
  });

  ipcMain.handle("resume-sync", () => {
    engine.resume();
    return { paused: false };
  });

  ipcMain.handle("check-tally", async () => {
    // The status bar fires this every 10s. While a sync/push is talking to
    // Tally, issuing another request would stack a second call onto the
    // single-threaded gateway — the classic c0000005 crash trigger. If Tally is
    // already busy, report the last-known status instead of probing again.
    if (tallyGate.isBusy()) {
      return { connected: lastTallyConnected, busy: true };
    }
    try {
      const tallyUrl = store.get("tallyUrl");
      await fetchTallyCompanies(tallyUrl);
      lastTallyConnected = true;
      return { connected: true };
    } catch (error: any) {
      lastTallyConnected = false;
      return { connected: false, error: error?.message || "Could not connect to TallyPrime." };
    }
  });
  ipcMain.handle("check-tally-capabilities", async () => {
    const tallyUrl = store.get("tallyUrl");
    const readMode = store.get("readMode", "auto");
    const odbcDsnOverride = store.get("odbcDsnOverride", "");

    // Don't open a competing XML + ODBC connection to single-threaded Tally while
    // a sync or push worker owns it — that's the concurrent-access condition that
    // crashes TallyPrime (c0000005). Report busy and let the user retry.
    if (tallyGate.isBusy()) {
      return {
        xml: { connected: false, error: "TallyBridge is busy with Tally — try again in a moment." },
        odbc: { state: "busy", dsn: null, supported_sections: [], message: "Tally is busy; capability check deferred." },
        readMode,
        transportPlan: {
          groups: "xml", ledgers: "xml", stock_items: "xml", vouchers: "xml",
          outstanding: "xml", profit_loss: "xml", balance_sheet: "xml", trial_balance: "xml",
        },
      };
    }

    let xmlConnected = false;
    let xmlError: string | null = null;
    try {
      await fetchTallyCompanies(tallyUrl);
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
      const companies = await fetchTallyCompanies(tallyUrl);

      return { success: true, companies };
    } catch (e: any) {
      console.error("get-tally-companies error:", e);
      return { success: false, companies: [], error: e?.message || "Could not read companies from Tally." };
    }
  });

  ipcMain.handle("open-log-file", () => {
    const logPath = logger.getLogPath();
    shell.showItemInFolder(logPath);
  });

  ipcMain.handle("get-startup-setting", () => {
    return app.getLoginItemSettings().openAtLogin;
  });

  ipcMain.handle("set-startup-setting", (_, enable: boolean) => {
    app.setLoginItemSettings({
      openAtLogin: enable,
      openAsHidden: true,
    });
    return { success: true };
  });

  ipcMain.handle("get-tally-company-date-ranges", async () => {
    try {
      const tallyUrl = store.get("tallyUrl");
      const companies = await fetchTallyCompanyDateRanges(tallyUrl);

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

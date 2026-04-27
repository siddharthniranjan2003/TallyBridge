import Store from "electron-store";
import { v4 as uuidv4 } from "uuid";

export interface Company {
  id: string;
  name: string;
  tallyGuid?: string;
  formalName?: string;
  enabled: boolean;
  addedAt: string;
  lastSyncedAt?: string;
  lastSyncStatus?: "success" | "error" | "syncing" | "idle";
  lastSyncRecords?: SyncRecordCounts;
  lastSyncError?: string;
  lastCompletedBackfillSignature?: string;
}

export interface SyncRecordCounts {
  groups: number;
  ledgers: number;
  vouchers: number;
  stock: number;
  outstanding: number;
  profit_loss: number;
  balance_sheet: number;
  trial_balance: number;
}

export type SyncIngestMode = "render" | "hybrid" | "direct";

export interface AppConfig {
  tallyUrl: string;
  syncIntervalMinutes: number;
  backendUrl: string;
  apiKey: string;
  controlPlaneUrl: string;
  controlPlaneApiKey: string;
  syncIngestMode: SyncIngestMode;
  syncIngestUrl: string;
  syncIngestKey: string;
  syncContractVersion: number;
  accountEmail: string;
  readMode: "auto" | "xml-only" | "hybrid";
  odbcDsnOverride: string;
  syncFromDate: string;
  syncToDate: string;
  companies: Company[];
}

export interface TallyCompanySelection {
  name: string;
  guid?: string;
  formalName?: string;
}

// Install uuid for store IDs: npm install uuid @types/uuid
export const store = new Store<AppConfig>({
  name: "tallybridge-config",
  defaults: {
    tallyUrl: "http://localhost:9000",
    syncIntervalMinutes: 5,
    backendUrl: "",
    apiKey: "",
    controlPlaneUrl: "",
    controlPlaneApiKey: "",
    syncIngestMode: "render",
    syncIngestUrl: "",
    syncIngestKey: "",
    syncContractVersion: 1,
    accountEmail: "",
    readMode: "auto",
    odbcDsnOverride: "",
    syncFromDate: "",
    syncToDate: "",
    companies: [],
  },
});

export function addCompany(selection: TallyCompanySelection): Company {
  const companies = store.get("companies");
  const newCompany: Company = {
    id: uuidv4(),
    name: selection.name,
    tallyGuid: selection.guid?.trim() || undefined,
    formalName: selection.formalName?.trim() || undefined,
    enabled: true,
    addedAt: new Date().toISOString(),
    lastSyncStatus: "idle",
  };
  store.set("companies", [...companies, newCompany]);
  return newCompany;
}

export function removeCompany(id: string) {
  const companies = store.get("companies").filter((c) => c.id !== id);
  store.set("companies", companies);
}

export function updateCompanyStatus(
  id: string,
  update: Partial<Company>
) {
  const companies = store.get("companies").map((c) =>
    c.id === id ? { ...c, ...update } : c
  );
  store.set("companies", companies);
}

export function resetStaleSyncStatuses() {
  const companies = store.get("companies");
  let changed = false;
  const nextCompanies = companies.map((company) => {
    if (company.lastSyncStatus !== "syncing") {
      return company;
    }

    changed = true;
    return {
      ...company,
      lastSyncStatus: "idle" as const,
    };
  });

  if (changed) {
    store.set("companies", nextCompanies);
  }

  return changed;
}

function normalizeNonEmptyString(value: unknown) {
  return typeof value === "string" ? value.trim() : "";
}

export function normalizeSyncIngestMode(value: unknown): SyncIngestMode {
  if (value === "direct") {
    return "direct";
  }
  if (value === "hybrid") {
    return "hybrid";
  }
  return "render";
}

export function normalizeSyncContractVersion(value: unknown) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : 1;
}

export function resolveControlPlaneUrl(config: Pick<AppConfig, "controlPlaneUrl" | "backendUrl">) {
  return normalizeNonEmptyString(config.controlPlaneUrl)
    || normalizeNonEmptyString(config.backendUrl);
}

export function resolveControlPlaneApiKey(config: Pick<AppConfig, "controlPlaneApiKey" | "apiKey">) {
  return normalizeNonEmptyString(config.controlPlaneApiKey)
    || normalizeNonEmptyString(config.apiKey);
}

export function resolveSyncIngestUrl(
  config: Pick<AppConfig, "syncIngestUrl">,
) {
  return normalizeNonEmptyString(config.syncIngestUrl);
}

export function resolveSyncIngestKey(
  config: Pick<AppConfig, "syncIngestKey">,
) {
  return normalizeNonEmptyString(config.syncIngestKey);
}

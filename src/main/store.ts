import Store from "electron-store";
import { v4 as uuidv4 } from "uuid";

export interface Company {
  id: string;
  name: string;
  enabled: boolean;
  addedAt: string;
  lastSyncedAt?: string;
  lastSyncStatus?: "success" | "error" | "syncing" | "idle";
  lastSyncRecords?: {
    ledgers: number;
    vouchers: number;
    stock: number;
    outstanding: number;
  };
  lastSyncError?: string;
}

export interface AppConfig {
  tallyUrl: string;
  syncIntervalMinutes: number;
  backendUrl: string;
  apiKey: string;
  accountEmail: string;
  companies: Company[];
}

// Install uuid for store IDs: npm install uuid @types/uuid
export const store = new Store<AppConfig>({
  name: "tallybridge-config",
  defaults: {
    tallyUrl: "http://localhost:9000",
    syncIntervalMinutes: 5,
    backendUrl: "",
    apiKey: "",
    accountEmail: "",
    companies: [],
  },
});

export function addCompany(name: string): Company {
  const companies = store.get("companies");
  const newCompany: Company = {
    id: uuidv4(),
    name,
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
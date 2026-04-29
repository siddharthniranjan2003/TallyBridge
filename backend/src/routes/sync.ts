import { Router, type Response } from "express";
import { supabase } from "../db/supabase.js";
import { requireApiKey } from "../middleware/auth.js";

const router = Router();
const BATCH_SIZE = 250;
const MAX_SECTION_ROWS = 100_000;
const SNAPSHOT_TIMESTAMP_COLUMNS = {
  outstanding: "last_outstanding_synced_at",
  profit_loss: "last_profit_loss_synced_at",
  balance_sheet: "last_balance_sheet_synced_at",
  trial_balance: "last_trial_balance_synced_at",
} as const;
const DEFAULT_ALLOWED_PUSH_VOUCHER_TYPES = [
  "Sales",
  "Purchase",
  "GST SALE",
  "GST PURCHASE",
] as const;
const DEFAULT_PUSH_QUEUE_BATCH_SIZE = 10;
const MAX_PUSH_QUEUE_BATCH_SIZE = 50;

type VoucherSyncMode = "full" | "incremental" | "none";

type SyncMeta = {
  voucher_sync_mode: VoucherSyncMode;
  voucher_from_date: string | null;
  voucher_to_date: string | null;
  effective_from_date: string | null;
  effective_to_date: string | null;
  date_range_source: "company_fy" | "override" | null;
  date_range_clamped: boolean;
  master_changed: boolean;
  voucher_changed: boolean;
  section_sources: Record<string, string>;
  product_name: string | null;
  product_version: string | null;
  odbc_status: Record<string, unknown> | null;
};

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function normalizeCompactDate(value: unknown) {
  if (!isNonEmptyString(value)) {
    return null;
  }

  const compact = value.trim();
  return /^\d{8}$/.test(compact) ? compact : null;
}

function normalizeTrimmedString(value: unknown) {
  return isNonEmptyString(value) ? value.trim() : null;
}

function compactDateToIsoDate(value: string | null) {
  if (!value) {
    return null;
  }

  return `${value.slice(0, 4)}-${value.slice(4, 6)}-${value.slice(6, 8)}`;
}

function normalizeVoucherTypeKey(value: unknown) {
  const normalized = normalizeTrimmedString(value);
  return normalized ? normalized.toUpperCase() : null;
}

function getAllowedPushVoucherTypeKeys() {
  const configured = process.env.TB_PUSH_ALLOWED_TYPES?.trim();
  const source = configured
    ? configured.split(",").map((entry) => entry.trim()).filter(Boolean)
    : [...DEFAULT_ALLOWED_PUSH_VOUCHER_TYPES];

  return new Set(source.map((entry) => entry.toUpperCase()));
}

function isAllowedPushVoucherType(value: unknown) {
  const normalized = normalizeVoucherTypeKey(value);
  return Boolean(normalized && getAllowedPushVoucherTypeKeys().has(normalized));
}

function normalizeIsoLikeDate(value: unknown) {
  const normalized = normalizeTrimmedString(value);
  if (!normalized) {
    return null;
  }

  if (/^\d{4}-\d{2}-\d{2}$/.test(normalized) || /^\d{8}$/.test(normalized)) {
    return normalized;
  }

  return null;
}

function normalizeFiniteNumber(value: unknown) {
  const numeric = typeof value === "number" ? value : Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function normalizeBoolean(value: unknown) {
  if (typeof value === "boolean") {
    return value;
  }

  if (typeof value === "string") {
    const normalized = value.trim().toLowerCase();
    if (["true", "yes", "1"].includes(normalized)) {
      return true;
    }
    if (["false", "no", "0"].includes(normalized)) {
      return false;
    }
  }

  return null;
}

function normalizePushVoucherPayload(value: unknown) {
  // PUSH PHASE 1: keep the backend strict so the new outbound path cannot enqueue
  // unsupported voucher shapes that would interfere with the stable inbound sync.
  const raw = value && typeof value === "object" ? (value as Record<string, unknown>) : null;
  if (!raw) {
    return { error: "voucher_payload must be an object" };
  }

  const voucherType = normalizeTrimmedString(raw.voucher_type);
  if (!voucherType) {
    return { error: "voucher_payload.voucher_type is required" };
  }
  if (!isAllowedPushVoucherType(voucherType)) {
    return {
      error: `voucher_payload.voucher_type must be one of: ${[...getAllowedPushVoucherTypeKeys()].join(", ")}`,
    };
  }

  const date = normalizeIsoLikeDate(raw.date);
  if (!date) {
    return { error: "voucher_payload.date must be in YYYY-MM-DD or YYYYMMDD format" };
  }

  const partyName = normalizeTrimmedString(raw.party_name);
  if (!partyName) {
    return { error: "voucher_payload.party_name is required" };
  }

  const rawLedgerEntries = raw.ledger_entries;
  if (!Array.isArray(rawLedgerEntries) || !rawLedgerEntries.length) {
    return { error: "voucher_payload.ledger_entries must be a non-empty array" };
  }

  const ledger_entries: Array<Record<string, unknown>> = [];
  for (const [index, ledgerEntry] of rawLedgerEntries.entries()) {
    const rawEntry = ledgerEntry && typeof ledgerEntry === "object"
      ? (ledgerEntry as Record<string, unknown>)
      : null;
    if (!rawEntry) {
      return { error: `voucher_payload.ledger_entries[${index}] must be an object` };
    }

    const ledgerName = normalizeTrimmedString(rawEntry.ledger_name);
    if (!ledgerName) {
      return { error: `voucher_payload.ledger_entries[${index}].ledger_name is required` };
    }

    const amount = normalizeFiniteNumber(rawEntry.amount);
    if (amount == null) {
      return { error: `voucher_payload.ledger_entries[${index}].amount must be numeric` };
    }

    const isDeemedPositive = normalizeBoolean(rawEntry.is_deemed_positive);
    if (isDeemedPositive == null) {
      return {
        error: `voucher_payload.ledger_entries[${index}].is_deemed_positive must be boolean`,
      };
    }

    ledger_entries.push({
      ledger_name: ledgerName,
      amount,
      is_deemed_positive: isDeemedPositive,
      is_party_ledger: normalizeBoolean(rawEntry.is_party_ledger) ?? false,
    });
  }

  const rawItems = raw.items;
  if (rawItems != null && !Array.isArray(rawItems)) {
    return { error: "voucher_payload.items must be an array when provided" };
  }

  const items: Array<Record<string, unknown>> = [];
  for (const [index, itemValue] of (rawItems || []).entries()) {
    const rawItem = itemValue && typeof itemValue === "object"
      ? (itemValue as Record<string, unknown>)
      : null;
    if (!rawItem) {
      return { error: `voucher_payload.items[${index}] must be an object` };
    }

    const stockItemName = normalizeTrimmedString(rawItem.stock_item_name);
    if (!stockItemName) {
      return { error: `voucher_payload.items[${index}].stock_item_name is required` };
    }

    const quantity = normalizeFiniteNumber(rawItem.quantity);
    if (quantity == null || quantity <= 0) {
      return { error: `voucher_payload.items[${index}].quantity must be greater than zero` };
    }

    const unit = normalizeTrimmedString(rawItem.unit);
    if (!unit) {
      return { error: `voucher_payload.items[${index}].unit is required` };
    }

    const rate = normalizeFiniteNumber(rawItem.rate);
    if (rate == null) {
      return { error: `voucher_payload.items[${index}].rate must be numeric` };
    }

    const amount = normalizeFiniteNumber(rawItem.amount);
    if (amount == null) {
      return { error: `voucher_payload.items[${index}].amount must be numeric` };
    }

    items.push({
      stock_item_name: stockItemName,
      quantity,
      unit,
      rate,
      amount,
      godown_name: normalizeTrimmedString(rawItem.godown_name),
      batch_name: normalizeTrimmedString(rawItem.batch_name),
      destination_godown_name: normalizeTrimmedString(rawItem.destination_godown_name),
    });
  }

  return {
    voucher: {
      date,
      voucher_type: voucherType,
      voucher_number: normalizeTrimmedString(raw.voucher_number),
      party_name: partyName,
      narration: normalizeTrimmedString(raw.narration),
      reference: normalizeTrimmedString(raw.reference),
      inventory_ledger_name: normalizeTrimmedString(raw.inventory_ledger_name),
      stock_ledger_name: normalizeTrimmedString(raw.stock_ledger_name),
      ledger_entries,
      items,
    },
  };
}

function normalizeSyncMeta(value: unknown): SyncMeta {
  const raw = value && typeof value === "object" ? (value as Record<string, unknown>) : {};
  const rawMode = raw.voucher_sync_mode;
  const voucher_sync_mode: VoucherSyncMode =
    rawMode === "incremental" || rawMode === "none" ? rawMode : "full";

  return {
    voucher_sync_mode,
    voucher_from_date: normalizeCompactDate(raw.voucher_from_date),
    voucher_to_date: normalizeCompactDate(raw.voucher_to_date),
    effective_from_date: normalizeCompactDate(raw.effective_from_date),
    effective_to_date: normalizeCompactDate(raw.effective_to_date),
    date_range_source:
      raw.date_range_source === "company_fy" || raw.date_range_source === "override"
        ? raw.date_range_source
        : null,
    date_range_clamped: raw.date_range_clamped === true,
    master_changed: raw.master_changed !== false,
    voucher_changed: raw.voucher_changed !== false,
    section_sources:
      raw.section_sources && typeof raw.section_sources === "object"
        ? Object.fromEntries(
            Object.entries(raw.section_sources as Record<string, unknown>).filter(
              ([, value]) => typeof value === "string"
            )
          ) as Record<string, string>
        : {},
    product_name: normalizeTrimmedString(raw.product_name),
    product_version: normalizeTrimmedString(raw.product_version),
    odbc_status:
      raw.odbc_status && typeof raw.odbc_status === "object"
        ? (raw.odbc_status as Record<string, unknown>)
        : null,
  };
}

function buildCompanyUpsertPayload(
  companyName: string,
  companyGuid: string | null,
  companyInfo: unknown,
  alterIds: unknown,
) {
  const payload: Record<string, unknown> = { name: companyName };
  if (companyGuid) {
    payload.guid = companyGuid;
  }

  if (companyInfo && typeof companyInfo === "object") {
    const info = companyInfo as Record<string, unknown>;
    for (const key of [
      "books_from",
      "books_to",
      "books_from_raw",
      "books_to_raw",
      "gstin",
      "address",
      "guid",
      "state",
      "country",
      "pincode",
      "email",
      "phone",
      "gst_type",
      "pan",
    ]) {
      if (key in info && info[key] !== "" && info[key] != null) {
        if (key === "guid" && companyGuid) {
          continue;
        }
        payload[key] = info[key];
      }
    }

    if (typeof info.master_id === "number" && Number.isFinite(info.master_id)) {
      payload.master_id = info.master_id;
    }
  }

  if (alterIds && typeof alterIds === "object") {
    const ids = alterIds as Record<string, unknown>;
    for (const key of ["alter_id", "alt_vch_id", "alt_mst_id", "last_voucher_date"]) {
      if (key in ids && ids[key] !== "" && ids[key] != null) {
        payload[key] = ids[key];
      }
    }
  }

  return payload;
}

function chunkArray<T>(items: T[], size = BATCH_SIZE): T[][] {
  const chunks: T[][] = [];
  for (let index = 0; index < items.length; index += size) {
    chunks.push(items.slice(index, index + size));
  }
  return chunks;
}

function buildRecordCounts(sections: Record<string, unknown>) {
  const counts: Record<string, unknown> = {
    groups: Array.isArray(sections.groups) ? sections.groups.length : undefined,
    ledgers: Array.isArray(sections.ledgers) ? sections.ledgers.length : undefined,
    vouchers: Array.isArray(sections.vouchers) ? sections.vouchers.length : undefined,
    purchases: Array.isArray(sections.purchases) ? sections.purchases.length : undefined,
    stock: Array.isArray(sections.stock_items) ? sections.stock_items.length : undefined,
    outstanding: Array.isArray(sections.outstanding) ? sections.outstanding.length : undefined,
    profit_loss: Array.isArray(sections.profit_loss) ? sections.profit_loss.length : undefined,
    balance_sheet: Array.isArray(sections.balance_sheet) ? sections.balance_sheet.length : undefined,
    trial_balance: Array.isArray(sections.trial_balance) ? sections.trial_balance.length : undefined,
  };

  return Object.fromEntries(
    Object.entries(counts).filter(([, value]) => typeof value === "number")
  ) as Record<string, number>;
}

function validateSectionArray(label: string, value: unknown) {
  if (value == null) {
    return null;
  }

  if (!Array.isArray(value)) {
    return `${label} must be an array when provided`;
  }

  if (value.length > MAX_SECTION_ROWS) {
    return `${label} exceeds the maximum allowed size of ${MAX_SECTION_ROWS} rows`;
  }

  return null;
}

async function upsertInBatches(
  table: string,
  rows: any[],
  onConflict: string,
  label: string
) {
  if (!rows.length) return;

  for (const chunk of chunkArray(rows)) {
    const { error } = await supabase.from(table).upsert(chunk, { onConflict });
    if (error) {
      throw new Error(`${label} upsert failed: ${error.message}`);
    }
  }
}

async function insertInBatches(table: string, rows: any[], label: string) {
  if (!rows.length) return;

  for (const chunk of chunkArray(rows)) {
    const { error } = await supabase.from(table).insert(chunk);
    if (error) {
      throw new Error(`${label} insert failed: ${error.message}`);
    }
  }
}

async function deleteByEq(table: string, column: string, value: string, label: string) {
  const { error } = await supabase.from(table).delete().eq(column, value);
  if (error) {
    throw new Error(`${label} delete failed: ${error.message}`);
  }
}

async function insertReturningIdsInBatches(table: string, rows: any[], label: string) {
  const insertedIds: string[] = [];
  if (!rows.length) {
    return { insertedIds, error: null as Error | null };
  }

  for (const chunk of chunkArray(rows)) {
    const { data, error } = await supabase.from(table).insert(chunk).select("id");
    if (error) {
      return {
        insertedIds,
        error: new Error(`${label} insert failed: ${error.message}`),
      };
    }

    if (!Array.isArray(data) || data.length !== chunk.length) {
      return {
        insertedIds,
        error: new Error(`${label} insert did not return all row ids`),
      };
    }

    for (const row of data) {
      if (typeof row.id !== "string") {
        return {
          insertedIds,
          error: new Error(`${label} insert returned a row without an id`),
        };
      }
      insertedIds.push(row.id);
    }
  }

  return { insertedIds, error: null as Error | null };
}

async function deleteCompanyRowsExceptSync(
  table: string,
  companyId: string,
  syncedAt: string,
  label: string,
) {
  const { error } = await supabase
    .from(table)
    .delete()
    .eq("company_id", companyId)
    .neq("synced_at", syncedAt);

  if (error) {
    throw new Error(`${label} cleanup failed: ${error.message}`);
  }
}

async function selectRowsByIn(table: string, column: string, values: string[], label: string) {
  const rows: any[] = [];
  if (!values.length) return rows;

  for (const chunk of chunkArray(values)) {
    const { data, error } = await supabase.from(table).select("*").in(column, chunk);
    if (error) {
      throw new Error(`${label} lookup failed: ${error.message}`);
    }

    rows.push(...(data || []));
  }

  return rows;
}

async function selectCompanyRowsByIn(
  table: string,
  companyId: string,
  column: string,
  values: string[],
  label: string,
) {
  const rows: any[] = [];
  if (!values.length) return rows;

  for (const chunk of chunkArray(values)) {
    const { data, error } = await supabase
      .from(table)
      .select("*")
      .eq("company_id", companyId)
      .in(column, chunk);
    if (error) {
      throw new Error(`${label} lookup failed: ${error.message}`);
    }

    rows.push(...(data || []));
  }

  return rows;
}

type VoucherGraphSnapshot = {
  voucherRows: any[];
  itemRows: any[];
  entryRows: any[];
  purchaseRows: any[];
};

type VoucherRestoreContext = {
  companyId: string;
  voucherGuids: string[];
  snapshot: VoucherGraphSnapshot;
};

async function rollbackInsertedRows(table: string, insertedIds: string[], label: string) {
  if (!insertedIds.length) return;

  try {
    await deleteByIn(table, "id", insertedIds, `${label} rollback`);
  } catch (error: any) {
    console.error(`[Sync] ${label} rollback warning: ${error.message}`);
  }
}

async function restoreRows(table: string, rows: any[], label: string) {
  if (!rows.length) return;

  const restorableRows = rows.map(({ id, ...row }) => row);
  try {
    await insertInBatches(table, restorableRows, `${label} restore`);
  } catch (error: any) {
    console.error(`[Sync] ${label} restore warning: ${error.message}`);
  }
}

async function cleanupSnapshotRows(table: string, companyId: string, syncedAt: string, label: string) {
  try {
    await deleteCompanyRowsExceptSync(table, companyId, syncedAt, label);
  } catch (error: any) {
    console.warn(`[Sync] ${label} cleanup warning: ${error.message}`);
  }
}

async function deleteByIn(table: string, column: string, values: string[], label: string) {
  if (!values.length) return;

  for (const chunk of chunkArray(values)) {
    const { error } = await supabase.from(table).delete().in(column, chunk);
    if (error) {
      throw new Error(`${label} delete failed: ${error.message}`);
    }
  }
}

async function getVoucherIdMap(companyId: string, tallyGuids: string[]) {
  const voucherIdMap = new Map<string, string>();

  for (const chunk of chunkArray(tallyGuids)) {
    const { data, error } = await supabase
      .from("vouchers")
      .select("id, tally_guid")
      .eq("company_id", companyId)
      .in("tally_guid", chunk);

    if (error) {
      throw new Error(`Voucher id lookup failed: ${error.message}`);
    }

    for (const row of data || []) {
      if (row.tally_guid && row.id) {
        voucherIdMap.set(row.tally_guid, row.id);
      }
    }
  }

  return voucherIdMap;
}

async function deleteVoucherGraph(voucherIds: string[]) {
  if (!voucherIds.length) return;

  await deleteByIn("voucher_items", "voucher_id", voucherIds, "Voucher items stale rows");
  await deleteByIn(
    "voucher_ledger_entries",
    "voucher_id",
    voucherIds,
    "Voucher ledger entries stale rows"
  );
  await deleteByIn("vouchers", "id", voucherIds, "Stale vouchers");
}

async function captureVoucherGraphSnapshot(
  companyId: string,
  voucherGuids: string[],
  purchaseGuids: string[],
): Promise<VoucherGraphSnapshot> {
  const uniqueVoucherGuids = [...new Set(voucherGuids.filter((value) => typeof value === "string" && value))];
  const uniquePurchaseGuids = [...new Set(purchaseGuids.filter((value) => typeof value === "string" && value))];

  const voucherRows = await selectCompanyRowsByIn(
    "vouchers",
    companyId,
    "tally_guid",
    uniqueVoucherGuids,
    "Voucher snapshot",
  );
  const voucherIds = voucherRows
    .map((row: any) => row.id)
    .filter((id: unknown): id is string => typeof id === "string");
  const itemRows = await selectRowsByIn("voucher_items", "voucher_id", voucherIds, "Voucher item snapshot");
  const entryRows = await selectRowsByIn(
    "voucher_ledger_entries",
    "voucher_id",
    voucherIds,
    "Voucher ledger entry snapshot",
  );
  const purchaseRows = await selectCompanyRowsByIn(
    "purchases",
    companyId,
    "tally_guid",
    uniquePurchaseGuids,
    "Purchase snapshot",
  );

  return {
    voucherRows,
    itemRows,
    entryRows,
    purchaseRows,
  };
}

async function restoreVoucherGraphSnapshot(
  companyId: string,
  voucherGuids: string[],
  snapshot: VoucherGraphSnapshot,
) {
  try {
    const currentVoucherIdMap = await getVoucherIdMap(companyId, voucherGuids);
    const currentVoucherIds = [...currentVoucherIdMap.values()];
    await deleteVoucherGraph(currentVoucherIds);

    if (!snapshot.voucherRows.length) {
      return;
    }

    const restorableVoucherRows = snapshot.voucherRows.map(({ id, ...row }) => row);
    await upsertInBatches("vouchers", restorableVoucherRows, "company_id,tally_guid", "Voucher restore");

    const restoredVoucherIdMap = await getVoucherIdMap(
      companyId,
      snapshot.voucherRows
        .map((row: any) => row.tally_guid)
        .filter((value: unknown): value is string => typeof value === "string" && value.length > 0),
    );
    const oldVoucherIdToGuid = new Map<string, string>();
    for (const row of snapshot.voucherRows) {
      if (typeof row?.id === "string" && typeof row?.tally_guid === "string" && row.tally_guid) {
        oldVoucherIdToGuid.set(row.id, row.tally_guid);
      }
    }

    const restorableItemRows = snapshot.itemRows
      .map(({ id, ...row }) => {
        const tallyGuid = oldVoucherIdToGuid.get(row.voucher_id);
        const restoredVoucherId = tallyGuid ? restoredVoucherIdMap.get(tallyGuid) : null;
        if (!restoredVoucherId) return null;
        return { ...row, voucher_id: restoredVoucherId };
      })
      .filter(Boolean);
    const restorableEntryRows = snapshot.entryRows
      .map(({ id, ...row }) => {
        const tallyGuid = oldVoucherIdToGuid.get(row.voucher_id);
        const restoredVoucherId = tallyGuid ? restoredVoucherIdMap.get(tallyGuid) : null;
        if (!restoredVoucherId) return null;
        return { ...row, voucher_id: restoredVoucherId };
      })
      .filter(Boolean);
    const restorablePurchaseRows = snapshot.purchaseRows.map(({ id, ...row }) => row);

    await insertInBatches("voucher_items", restorableItemRows, "Voucher item restore");
    await insertInBatches("voucher_ledger_entries", restorableEntryRows, "Voucher ledger entry restore");
    await upsertInBatches("purchases", restorablePurchaseRows, "company_id,tally_guid", "Purchase restore");
  } catch (error: any) {
    console.error(`[Sync] Voucher graph restore warning: ${error.message}`);
  }
}

async function selectVouchersForReconciliation(companyId: string, syncMeta: SyncMeta) {
  let query = supabase
    .from("vouchers")
    .select("id, tally_guid")
    .eq("company_id", companyId);

  if (syncMeta.voucher_sync_mode === "incremental") {
    const fromIso = compactDateToIsoDate(syncMeta.voucher_from_date);
    const toIso = compactDateToIsoDate(syncMeta.voucher_to_date);

    if (!fromIso || !toIso) {
      return [];
    }

    query = query.gte("date", fromIso).lte("date", toIso);
  }

  const { data, error } = await query;
  if (error) {
    throw new Error(`Voucher reconciliation lookup failed: ${error.message}`);
  }

  return data || [];
}

async function reconcileVoucherScope(
  companyId: string,
  incomingTallyGuids: string[],
  syncMeta: SyncMeta,
) {
  if (syncMeta.voucher_sync_mode === "none") {
    return;
  }

  const existingRows = await selectVouchersForReconciliation(companyId, syncMeta);
  const incomingGuidSet = new Set(incomingTallyGuids);
  const staleVoucherIds = existingRows
    .filter((row: any) => !incomingGuidSet.has(row.tally_guid))
    .map((row: any) => row.id)
    .filter((id: unknown): id is string => typeof id === "string");

  await deleteVoucherGraph(staleVoucherIds);
}

const PURCHASE_VOUCHER_TYPES = new Set([
  "purchase",
  "local purchase",
  "import purchase",
]);

function isPurchaseVoucherType(value: unknown) {
  if (typeof value !== "string") {
    return false;
  }
  const normalised = value.trim().toLowerCase();
  // Exact match first
  if (PURCHASE_VOUCHER_TYPES.has(normalised)) {
    return true;
  }
  // Accept custom types that contain "purchase" but exclude orders
  return normalised.includes("purchase") && !normalised.includes("order");
}

const INVENTORY_REPORT_META = {
  ACT_NOW: { id: "R1", name: "ACT NOW" },
  HERO_SKU_HEALTH: { id: "R2", name: "HERO SKU HEALTH" },
  DEAD_CAPITAL: { id: "R3", name: "DEAD CAPITAL" },
  BUYING_MISTAKES: { id: "R4", name: "BUYING MISTAKES" },
  WIND_DOWN: { id: "R5", name: "WIND-DOWN" },
  RISK_WATCH: { id: "R6", name: "RISK WATCH" },
  FULL_PORTFOLIO_HEALTH: { id: "R7", name: "FULL PORTFOLIO HEALTH" },
} as const;

type InventoryReportKey = keyof typeof INVENTORY_REPORT_META;
type InventoryReportId = (typeof INVENTORY_REPORT_META)[InventoryReportKey]["id"];

const INVENTORY_REPORT_KEYS = Object.keys(INVENTORY_REPORT_META) as InventoryReportKey[];

const INVENTORY_SCENARIO_META = {
  GHOST_ZERO: {
    name: "GHOST ZERO",
    color: "red",
    priority: 15,
    reportKeys: ["DEAD_CAPITAL", "FULL_PORTFOLIO_HEALTH"],
  },
  INERT: {
    name: "INERT",
    color: "orange",
    priority: 14,
    reportKeys: ["DEAD_CAPITAL", "RISK_WATCH", "FULL_PORTFOLIO_HEALTH"],
  },
  ONSET: {
    name: "ONSET",
    color: "green",
    priority: 10,
    reportKeys: ["BUYING_MISTAKES", "FULL_PORTFOLIO_HEALTH"],
  },
  OFF_BOOK: {
    name: "OFF-BOOK",
    color: "red",
    priority: 13,
    reportKeys: ["DEAD_CAPITAL", "BUYING_MISTAKES", "FULL_PORTFOLIO_HEALTH"],
  },
  DEAD: {
    name: "DEAD",
    color: "red",
    priority: 11,
    reportKeys: ["DEAD_CAPITAL", "BUYING_MISTAKES", "FULL_PORTFOLIO_HEALTH"],
  },
  BLAZE: {
    name: "BLAZE",
    color: "green",
    priority: 6,
    reportKeys: ["HERO_SKU_HEALTH", "FULL_PORTFOLIO_HEALTH"],
  },
  TAPER: {
    name: "TAPER",
    color: "orange",
    priority: 8,
    reportKeys: ["WIND_DOWN", "FULL_PORTFOLIO_HEALTH"],
  },
  SURGE: {
    name: "SURGE",
    color: "green",
    priority: 3,
    reportKeys: ["ACT_NOW", "HERO_SKU_HEALTH", "FULL_PORTFOLIO_HEALTH"],
  },
  DRAIN: {
    name: "DRAIN",
    color: "orange",
    priority: 9,
    reportKeys: ["WIND_DOWN", "RISK_WATCH", "FULL_PORTFOLIO_HEALTH"],
  },
  STARVE_ZERO: {
    name: "STARVE ZERO",
    color: "red",
    priority: 1,
    reportKeys: ["ACT_NOW", "FULL_PORTFOLIO_HEALTH"],
  },
  STARVE_CRITICAL: {
    name: "STARVE CRITICAL",
    color: "red",
    priority: 2,
    reportKeys: ["ACT_NOW", "FULL_PORTFOLIO_HEALTH"],
  },
  STARVE_WATCH: {
    name: "STARVE WATCH",
    color: "orange",
    priority: 5,
    reportKeys: ["ACT_NOW", "FULL_PORTFOLIO_HEALTH"],
  },
  BLOAT: {
    name: "BLOAT",
    color: "red",
    priority: 12,
    reportKeys: ["DEAD_CAPITAL", "BUYING_MISTAKES", "RISK_WATCH", "FULL_PORTFOLIO_HEALTH"],
  },
  FLOW: {
    name: "FLOW",
    color: "green",
    priority: 7,
    reportKeys: ["HERO_SKU_HEALTH", "FULL_PORTFOLIO_HEALTH"],
  },
  PINCH: {
    name: "PINCH",
    color: "orange",
    priority: 4,
    reportKeys: ["ACT_NOW", "RISK_WATCH", "FULL_PORTFOLIO_HEALTH"],
  },
} as const satisfies Record<string, {
  name: string;
  color: string;
  priority: number;
  reportKeys: readonly InventoryReportKey[];
}>;

type InventoryScenarioKey = keyof typeof INVENTORY_SCENARIO_META;

const INVENTORY_REORDER_SCENARIOS = new Set<InventoryScenarioKey>([
  "BLAZE",
  "SURGE",
  "PINCH",
  "STARVE_ZERO",
  "STARVE_CRITICAL",
  "STARVE_WATCH",
]);

type InventoryIntelligenceItem = {
  stock_item_name: string;
  unit: string | null;
  scenario: InventoryScenarioKey;
  scenario_name: string;
  color: string;
  priority: number;
  report_ids: InventoryReportId[];
  report_keys: InventoryReportKey[];
  report_names: string[];
  avg_sale_6m: number;
  last_month_purchase: number;
  closing_stock_value: number;
  sales_qty_6m_avg: number;
  purchase_qty_1m: number;
  closing_stock_qty: number;
  purchase_rate: number | null;
  sales_amount: number | null;
  purchase_amount: number | null;
  closing_stock_amount: number | null;
};

function isIsoDateString(value: string) {
  return /^\d{4}-\d{2}-\d{2}$/.test(value);
}

function shiftIsoDate(value: string, days: number) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) {
    throw new Error(`Invalid ISO date: ${value}`);
  }

  const [, yearRaw, monthRaw, dayRaw] = match;
  const year = Number(yearRaw);
  const month = Number(monthRaw);
  const day = Number(dayRaw);
  const date = new Date(Date.UTC(year, month - 1, day));
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

function toMoney(value: unknown) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return 0;
  }
  return Number(numeric.toFixed(2));
}

function toPaise(value: unknown) {
  return Math.round(toMoney(value) * 100);
}

function parseThresholdValue(value: unknown) {
  const normalized = normalizeTrimmedString(value);
  if (!normalized) {
    return null;
  }

  const parsed = Number(normalized);
  if (!Number.isFinite(parsed) || parsed < 0) {
    throw new Error("threshold must be a non-negative number");
  }

  return parsed;
}

function parseLimitValue(value: unknown) {
  const normalized = normalizeTrimmedString(value);
  if (!normalized) {
    return null;
  }

  const parsed = Number(normalized);
  if (!Number.isInteger(parsed) || parsed <= 0) {
    throw new Error("limit must be a positive integer");
  }

  return parsed;
}

type InventoryResponseFormat = "json" | "csv";

function parseInventoryResponseFormat(value: unknown): InventoryResponseFormat {
  const normalized = normalizeTrimmedString(value);
  if (!normalized) {
    return "json";
  }

  const format = normalized.toLowerCase();
  if (format === "json" || format === "csv") {
    return format;
  }

  throw new Error("format must be either json or csv");
}

const INVENTORY_CSV_COLUMNS = [
  "stock_item_name",
  "sales_qty_6m_avg",
  "purchase_qty_1m",
  "closing_stock_qty",
  "purchase_rate",
  "sales_amount",
  "purchase_amount",
  "closing_stock_amount",
  "scenario_name",
] as const;

function escapeCsvValue(value: unknown) {
  if (value == null) {
    return "";
  }

  const text = String(value);
  if (!/[",\r\n]/.test(text)) {
    return text;
  }

  return `"${text.replace(/"/g, '""')}"`;
}

function serializeInventoryItemsToCsv(items: InventoryIntelligenceItem[]) {
  const header = INVENTORY_CSV_COLUMNS.join(",");
  const rows = items.map((item) => INVENTORY_CSV_COLUMNS
    .map((column) => escapeCsvValue(item[column]))
    .join(","));

  return [header, ...rows].join("\r\n");
}

function sanitizeCsvToken(value: string | null) {
  const normalized = normalizeTrimmedString(value);
  if (!normalized) {
    return "all";
  }

  return normalized.replace(/[^A-Z0-9]+/gi, "_").replace(/^_+|_+$/g, "") || "all";
}

function normalizeReportFilterToken(value: string) {
  return value
    .trim()
    .toUpperCase()
    .replace(/[^A-Z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

const INVENTORY_REPORT_FILTER_ALIASES = new Map<string, InventoryReportKey>(
  (Object.entries(INVENTORY_REPORT_META) as Array<
    [InventoryReportKey, (typeof INVENTORY_REPORT_META)[InventoryReportKey]]
  >).flatMap(([reportKey, meta]) => ([
    [normalizeReportFilterToken(reportKey), reportKey],
    [normalizeReportFilterToken(meta.id), reportKey],
    [normalizeReportFilterToken(meta.name), reportKey],
  ])),
);

function normalizeInventoryReportKey(value: unknown) {
  const normalized = normalizeTrimmedString(value);
  if (!normalized) {
    return null;
  }

  return INVENTORY_REPORT_FILTER_ALIASES.get(normalizeReportFilterToken(normalized)) ?? null;
}

async function getLatestCompanyVoucherDate(companyId: string) {
  const { data, error } = await supabase
    .from("vouchers")
    .select("date, id")
    .eq("company_id", companyId)
    .not("date", "is", null)
    .order("date", { ascending: false })
    .order("id", { ascending: false })
    .limit(1)
    .maybeSingle();

  if (error) {
    throw new Error(`Latest voucher date lookup failed: ${error.message}`);
  }

  const latestDate = normalizeTrimmedString(data?.date);
  return latestDate && isIsoDateString(latestDate) ? latestDate : null;
}

type VoucherItemMetrics = {
  amountRaw: number;
  quantityRaw: number;
};

async function aggregateVoucherItemMetricsByName(voucherIds: string[], label: string) {
  const metricsByItem = new Map<string, VoucherItemMetrics>();
  if (!voucherIds.length) {
    return metricsByItem;
  }

  for (const chunk of chunkArray(voucherIds, 100)) {
    const rows = await fetchAllPages(`${label} rows`, (from, to) =>
      supabase
        .from("voucher_items")
        .select("id, stock_item_name, amount, quantity")
        .in("voucher_id", chunk)
        .order("id", { ascending: true })
        .range(from, to),
    );

    for (const row of rows) {
      const stockItemName = normalizeTrimmedString((row as any).stock_item_name);
      if (!stockItemName) {
        continue;
      }

      const amount = Math.abs(Number((row as any).amount) || 0);
      const quantity = Math.abs(Number((row as any).quantity) || 0);
      const current = metricsByItem.get(stockItemName) ?? { amountRaw: 0, quantityRaw: 0 };
      metricsByItem.set(stockItemName, {
        amountRaw: current.amountRaw + amount,
        quantityRaw: current.quantityRaw + quantity,
      });
    }
  }

  return metricsByItem;
}

function classifyInventoryScenarioV2(
  avgSale6mPaise: number,
  lastMonthPurchasePaise: number,
  closingStockPaise: number,
): InventoryScenarioKey | null {
  if (avgSale6mPaise === 0) {
    if (lastMonthPurchasePaise === 0) {
      return closingStockPaise === 0 ? "GHOST_ZERO" : "INERT";
    }
    if (closingStockPaise < lastMonthPurchasePaise) {
      return "OFF_BOOK";
    }
    if (closingStockPaise <= lastMonthPurchasePaise * 2) {
      return "ONSET";
    }
    return "DEAD";
  }

  if (lastMonthPurchasePaise === 0) {
    if (closingStockPaise === 0) {
      return "STARVE_ZERO";
    }
    if (closingStockPaise <= avgSale6mPaise) {
      return closingStockPaise * 2 < avgSale6mPaise ? "STARVE_CRITICAL" : "STARVE_WATCH";
    }
    return "DRAIN";
  }

  if (closingStockPaise === 0) {
    if (lastMonthPurchasePaise < avgSale6mPaise && avgSale6mPaise < lastMonthPurchasePaise * 2) {
      return "BLAZE";
    }
    if (lastMonthPurchasePaise * 2 < avgSale6mPaise) {
      return "TAPER";
    }
    if (lastMonthPurchasePaise * 2 > avgSale6mPaise * 3 && lastMonthPurchasePaise < avgSale6mPaise * 3) {
      return "SURGE";
    }
    return null;
  }

  if (closingStockPaise > avgSale6mPaise * 4) {
    return "BLOAT";
  }
  if (closingStockPaise >= avgSale6mPaise * 2) {
    return "FLOW";
  }
  return "PINCH";
}

function withSupabaseSchemaGuidance(message: string) {
  const normalized = (message || "").toLowerCase();
  const looksLikeSchemaProblem =
    normalized.includes("schema cache")
    || normalized.includes("could not find the table")
    || normalized.includes("could not find the '")
    || normalized.includes("relation ")
    || normalized.includes("column ");

  if (!looksLikeSchemaProblem || normalized.includes("apply backend/full_schema.sql")) {
    return message;
  }

  return `${message}. Supabase schema appears missing or outdated. Apply backend/full_schema.sql to the new Supabase project, then restart the backend.`;
}

async function buildInventoryIntelligenceReport(
  {
    companyId,
    companyGuid,
    companyName,
    asOfDateInput,
    thresholdInput,
    limitInput,
    reportKeyFilter,
  }: {
    companyId: string;
    companyGuid: string | null;
    companyName: string | null;
    asOfDateInput: unknown;
    thresholdInput: unknown;
    limitInput: unknown;
    reportKeyFilter?: InventoryReportKey | null;
  },
) {
  const requestedAsOfDate = normalizeTrimmedString(asOfDateInput);
  if (requestedAsOfDate && !isIsoDateString(requestedAsOfDate)) {
    throw new Error("as_of_date must be in YYYY-MM-DD format");
  }

  const asOfDate = requestedAsOfDate ?? await getLatestCompanyVoucherDate(companyId);
  if (!asOfDate) {
    throw new Error("No voucher date available for this company");
  }

  const threshold = parseThresholdValue(thresholdInput);
  const limit = parseLimitValue(limitInput);

  const saleWindowFrom = shiftIsoDate(asOfDate, -179);
  const saleWindowTo = asOfDate;
  const purchaseWindowFrom = shiftIsoDate(asOfDate, -29);
  const purchaseWindowTo = asOfDate;

  const saleVoucherRows = await fetchAllPages("Inventory GST SALE vouchers", (from, to) =>
    supabase
      .from("vouchers")
      .select("id")
      .eq("company_id", companyId)
      .eq("voucher_type", "GST SALE")
      .eq("is_cancelled", false)
      .gte("date", saleWindowFrom)
      .lte("date", saleWindowTo)
      .order("id", { ascending: true })
      .range(from, to),
  );

  const purchaseVoucherRows = await fetchAllPages("Inventory purchase vouchers", (from, to) =>
    supabase
      .from("vouchers")
      .select("id, voucher_type")
      .eq("company_id", companyId)
      .eq("is_cancelled", false)
      .gte("date", purchaseWindowFrom)
      .lte("date", purchaseWindowTo)
      .order("id", { ascending: true })
      .range(from, to),
  );

  const purchaseVoucherIds = purchaseVoucherRows
    .filter((row: any) => typeof row?.id === "string" && isPurchaseVoucherType(row?.voucher_type))
    .map((row: any) => row.id as string);

  const [saleMetricsByItem, purchaseMetricsByItem, stockItems] = await Promise.all([
    aggregateVoucherItemMetricsByName(
      saleVoucherRows
        .map((row: any) => row?.id)
        .filter((value: unknown): value is string => typeof value === "string" && value.length > 0),
      "Inventory GST SALE voucher items",
    ),
    aggregateVoucherItemMetricsByName(purchaseVoucherIds, "Inventory purchase voucher items"),
    fetchAllPages("Inventory stock items", (from, to) =>
      supabase
        .from("stock_items")
        .select("id, name, unit, closing_qty, closing_value")
        .eq("company_id", companyId)
        .order("name", { ascending: true })
        .order("id", { ascending: true })
        .range(from, to),
    ),
  ]);

  const stockItemsByName = new Map<string, {
    unit: string | null;
    closingQuantityRaw: number;
    closingStockRaw: number;
  }>();

  for (const stockItem of stockItems) {
    const stockItemName = normalizeTrimmedString((stockItem as any).name);
    if (!stockItemName) {
      continue;
    }

    stockItemsByName.set(stockItemName, {
      unit: normalizeTrimmedString((stockItem as any).unit),
      closingQuantityRaw: Number((stockItem as any).closing_qty) || 0,
      closingStockRaw: Math.abs(Number((stockItem as any).closing_value) || 0),
    });
  }

  const allItemNames = new Set<string>([
    ...stockItemsByName.keys(),
    ...saleMetricsByItem.keys(),
    ...purchaseMetricsByItem.keys(),
  ]);

  const allClassifiedItems: InventoryIntelligenceItem[] = [];
  let unclassifiedCount = 0;

  for (const stockItemName of allItemNames) {
    const stockSnapshot = stockItemsByName.get(stockItemName);
    const saleMetrics = saleMetricsByItem.get(stockItemName) ?? { amountRaw: 0, quantityRaw: 0 };
    const purchaseMetrics = purchaseMetricsByItem.get(stockItemName) ?? { amountRaw: 0, quantityRaw: 0 };
    const totalSaleAmountRaw = saleMetrics.amountRaw;
    const totalSaleQuantityRaw = saleMetrics.quantityRaw;
    const avgSale6mRaw = totalSaleAmountRaw / 6;
    const avgSaleQuantity6mRaw = totalSaleQuantityRaw / 6;
    const lastMonthPurchaseRaw = purchaseMetrics.amountRaw;
    const purchaseQuantity1mRaw = purchaseMetrics.quantityRaw;
    const closingQuantityRaw = stockSnapshot?.closingQuantityRaw ?? 0;
    const closingStockRaw = stockSnapshot?.closingStockRaw ?? 0;
    const purchaseRateRaw = purchaseQuantity1mRaw > 0
      ? lastMonthPurchaseRaw / purchaseQuantity1mRaw
      : null;
    const salesAmountRaw = purchaseRateRaw == null ? null : avgSaleQuantity6mRaw * purchaseRateRaw;
    const purchaseAmountRaw = purchaseRateRaw == null ? null : purchaseQuantity1mRaw * purchaseRateRaw;
    const closingStockAmountRaw = purchaseRateRaw == null ? null : closingQuantityRaw * purchaseRateRaw;
    const avgSale6mPaise = toPaise(avgSale6mRaw);
    const lastMonthPurchasePaise = toPaise(lastMonthPurchaseRaw);
    const closingStockPaise = toPaise(closingStockRaw);

    const scenario = classifyInventoryScenarioV2(
      avgSale6mPaise,
      lastMonthPurchasePaise,
      closingStockPaise,
    );
    if (!scenario) {
      unclassifiedCount += 1;
      continue;
    }

    const meta = INVENTORY_SCENARIO_META[scenario];
    const reportKeys = [...meta.reportKeys];
    const reportIds = reportKeys.map((reportKey) => INVENTORY_REPORT_META[reportKey].id);
    const reportNames = reportKeys.map((reportKey) => INVENTORY_REPORT_META[reportKey].name);

    allClassifiedItems.push({
      stock_item_name: stockItemName,
      unit: stockSnapshot?.unit ?? null,
      scenario,
      scenario_name: meta.name,
      color: meta.color,
      priority: meta.priority,
      report_ids: reportIds,
      report_keys: reportKeys,
      report_names: reportNames,
      avg_sale_6m: toMoney(avgSale6mRaw),
      last_month_purchase: toMoney(lastMonthPurchaseRaw),
      closing_stock_value: toMoney(closingStockRaw),
      sales_qty_6m_avg: toMoney(avgSaleQuantity6mRaw),
      purchase_qty_1m: toMoney(purchaseQuantity1mRaw),
      closing_stock_qty: toMoney(closingQuantityRaw),
      purchase_rate: purchaseRateRaw == null ? null : toMoney(purchaseRateRaw),
      sales_amount: salesAmountRaw == null ? null : toMoney(salesAmountRaw),
      purchase_amount: purchaseAmountRaw == null ? null : toMoney(purchaseAmountRaw),
      closing_stock_amount: closingStockAmountRaw == null ? null : toMoney(closingStockAmountRaw),
    });
  }

  allClassifiedItems.sort((left, right) => {
    if (left.priority !== right.priority) {
      return left.priority - right.priority;
    }
    return left.stock_item_name.localeCompare(right.stock_item_name);
  });

  const thresholdPaise = threshold == null ? null : toPaise(threshold);
  const thresholdFilteredItems = thresholdPaise == null
    ? allClassifiedItems
    : allClassifiedItems.filter((item) => (
      toPaise(item.sales_amount ?? 0) >= thresholdPaise
      || toPaise(item.purchase_amount ?? 0) >= thresholdPaise
      || toPaise(item.closing_stock_amount ?? 0) >= thresholdPaise
    ));

  const reportCounts = Object.fromEntries(
    INVENTORY_REPORT_KEYS.map((reportKey) => [reportKey, 0]),
  ) as Record<InventoryReportKey, number>;

  for (const item of thresholdFilteredItems) {
    for (const reportKey of item.report_keys) {
      reportCounts[reportKey] += 1;
    }
  }

  const filteredItems = reportKeyFilter
    ? thresholdFilteredItems.filter((item) => item.report_keys.includes(reportKeyFilter))
    : thresholdFilteredItems;
  const limitedItems = limit ? filteredItems.slice(0, limit) : filteredItems;
  const activeReportMeta = reportKeyFilter ? INVENTORY_REPORT_META[reportKeyFilter] : null;

  return {
    company_id: companyId,
    company_guid: companyGuid,
    company_name: companyName,
    report_filter: reportKeyFilter ?? null,
    report_filter_id: activeReportMeta?.id ?? null,
    report_filter_name: activeReportMeta?.name ?? null,
    as_of_date: asOfDate,
    sale_window_from: saleWindowFrom,
    sale_window_to: saleWindowTo,
    purchase_window_from: purchaseWindowFrom,
    purchase_window_to: purchaseWindowTo,
    threshold,
    limit,
    total_items_scanned: allItemNames.size,
    total_classified_count: thresholdFilteredItems.length,
    classified_count: filteredItems.length,
    unclassified_count: unclassifiedCount,
    needs_reorder_count: filteredItems.filter((item) =>
      INVENTORY_REORDER_SCENARIOS.has(item.scenario)
    ).length,
    returned_count: limitedItems.length,
    available_reports: INVENTORY_REPORT_KEYS.map((reportKey) => ({
      report_key: reportKey,
      report_id: INVENTORY_REPORT_META[reportKey].id,
      report_name: INVENTORY_REPORT_META[reportKey].name,
      count: reportCounts[reportKey],
    })),
    items: limitedItems,
  };
}

type InventoryIntelligenceReport = Awaited<ReturnType<typeof buildInventoryIntelligenceReport>>;

function sendInventoryIntelligenceResponse(
  res: Response,
  report: InventoryIntelligenceReport,
  format: InventoryResponseFormat,
) {
  if (format === "csv") {
    const reportToken = sanitizeCsvToken(report.report_filter_id ?? report.report_filter ?? null);
    const companyToken = sanitizeCsvToken(report.company_name);
    const dateToken = sanitizeCsvToken(report.as_of_date);
    const filename = `reorder-levels-${reportToken}-${companyToken}-${dateToken}.csv`;

    res.setHeader("Content-Type", "text/csv; charset=utf-8");
    res.setHeader("Content-Disposition", `attachment; filename="${filename}"`);
    return res.status(200).send(serializeInventoryItemsToCsv(report.items));
  }

  return res.json(report);
}

async function selectPurchasesForReconciliation(companyId: string, syncMeta: SyncMeta) {
  let fromIso: string | null = null;
  let toIso: string | null = null;

  if (syncMeta.voucher_sync_mode === "incremental") {
    fromIso = compactDateToIsoDate(syncMeta.voucher_from_date);
    toIso = compactDateToIsoDate(syncMeta.voucher_to_date);
    if (!fromIso || !toIso) {
      return [];
    }
  }

  return fetchAllPages("Purchase reconciliation", (from, to) => {
    let query = supabase
      .from("purchases")
      .select("id, tally_guid")
      .eq("company_id", companyId)
      .order("date", { ascending: false })
      .order("id", { ascending: false })
      .range(from, to);
    if (fromIso && toIso) {
      query = query.gte("date", fromIso).lte("date", toIso);
    }
    return query;
  });
}

async function reconcilePurchaseScope(
  companyId: string,
  incomingTallyGuids: string[],
  syncMeta: SyncMeta,
) {
  if (syncMeta.voucher_sync_mode === "none") {
    return;
  }

  const existingRows = await selectPurchasesForReconciliation(companyId, syncMeta);
  const incomingGuidSet = new Set(incomingTallyGuids);
  const stalePurchaseIds = existingRows
    .filter((row: any) => !incomingGuidSet.has(row.tally_guid))
    .map((row: any) => row.id)
    .filter((id: unknown): id is string => typeof id === "string");

  await deleteByIn("purchases", "id", stalePurchaseIds, "Stale purchases");
}

const COMPANY_LOOKUP_COLUMNS = `
  id,
  name,
  guid,
  last_synced_at,
  last_outstanding_synced_at,
  last_profit_loss_synced_at,
  last_balance_sheet_synced_at,
  last_trial_balance_synced_at
`;

function toResolvedCompanyLookup(company: any) {
  return {
    status: 200 as const,
    companyId: company.id,
    companyName: company.name ?? null,
    companyGuid: company.guid ?? null,
    lastSyncedAt: company.last_synced_at ?? null,
    lastOutstandingSyncedAt: company.last_outstanding_synced_at ?? null,
    lastProfitLossSyncedAt: company.last_profit_loss_synced_at ?? null,
    lastBalanceSheetSyncedAt: company.last_balance_sheet_synced_at ?? null,
    lastTrialBalanceSyncedAt: company.last_trial_balance_synced_at ?? null,
  };
}

async function resolveCompanyLookup(
  {
    companyId,
    companyGuid,
    companyName,
  }: {
    companyId?: unknown;
    companyGuid?: unknown;
    companyName?: unknown;
  },
  options?: { requireSuccessfulSync?: boolean },
) {
  const requireSuccessfulSync = options?.requireSuccessfulSync !== false;
  const normalizedCompanyId = normalizeTrimmedString(companyId);
  const normalizedCompanyGuid = normalizeTrimmedString(companyGuid);
  const normalizedCompanyName = normalizeTrimmedString(companyName);

  if (normalizedCompanyId) {
    const { data: company, error } = await supabase
      .from("companies")
      .select(COMPANY_LOOKUP_COLUMNS)
      .eq("id", normalizedCompanyId)
      .maybeSingle();

    if (error || !company) {
      return { status: 404 as const, error: "Company not found" };
    }

    if (requireSuccessfulSync && !company.last_synced_at) {
      return { status: 409 as const, error: "Company has not completed a successful sync yet" };
    }

    return toResolvedCompanyLookup(company);
  }

  if (normalizedCompanyGuid) {
    const { data: company, error } = await supabase
      .from("companies")
      .select(COMPANY_LOOKUP_COLUMNS)
      .eq("guid", normalizedCompanyGuid)
      .maybeSingle();

    if (error || !company) {
      return { status: 404 as const, error: "Company not found" };
    }

    if (requireSuccessfulSync && !company.last_synced_at) {
      return { status: 409 as const, error: "Company has not completed a successful sync yet" };
    }

    return toResolvedCompanyLookup(company);
  }

  if (!normalizedCompanyName) {
    return {
      status: 400 as const,
      error: "company_id, company_guid, or company_name query param required",
    };
  }

  const { data: companies, error } = await supabase
    .from("companies")
    .select(COMPANY_LOOKUP_COLUMNS)
    .eq("name", normalizedCompanyName)
    .limit(2);

  if (error || !companies?.length) {
    return { status: 404 as const, error: "Company not found" };
  }

  if (companies.length > 1) {
    return {
      status: 409 as const,
      error: "Multiple companies share this name. Use company_guid or company_id instead.",
    };
  }

  const company = companies[0]!;
  if (requireSuccessfulSync && !company.last_synced_at) {
    return { status: 409 as const, error: "Company has not completed a successful sync yet" };
  }

  return toResolvedCompanyLookup(company);
}

async function upsertCompanyRecord(
  companyName: string,
  companyGuid: string | null,
  companyInfo: unknown,
  alterIds: unknown,
) {
  const payload = buildCompanyUpsertPayload(companyName, companyGuid, companyInfo, alterIds);

  if (companyGuid) {
    const { data: existingByGuid, error: guidLookupError } = await supabase
      .from("companies")
      .select("id")
      .eq("guid", companyGuid)
      .maybeSingle();

    if (guidLookupError) {
      throw new Error(`Company GUID lookup failed: ${guidLookupError.message}`);
    }

    if (existingByGuid?.id) {
      const { data: updatedCompany, error: updateError } = await supabase
        .from("companies")
        .update(payload)
        .eq("id", existingByGuid.id)
        .select("id")
        .single();

      if (updateError || !updatedCompany) {
        throw new Error(`Company update failed: ${updateError?.message}`);
      }

      return updatedCompany;
    }
  }

  const { data: nameMatches, error: nameLookupError } = await supabase
    .from("companies")
    .select("id, guid")
    .eq("name", companyName)
    .limit(2);

  if (nameLookupError) {
    throw new Error(`Company name lookup failed: ${nameLookupError.message}`);
  }

  if ((nameMatches || []).length > 1) {
    throw new Error(
      "Multiple companies already share this name. Re-add the company so sync can use the Tally GUID."
    );
  }

  const matchedCompany = nameMatches?.[0];
  if (matchedCompany?.id && (!matchedCompany.guid || matchedCompany.guid === companyGuid)) {
    const { data: updatedCompany, error: updateError } = await supabase
      .from("companies")
      .update(payload)
      .eq("id", matchedCompany.id)
      .select("id")
      .single();

    if (updateError || !updatedCompany) {
      throw new Error(`Company update failed: ${updateError?.message}`);
    }

    return updatedCompany;
  }

  const { data: insertedCompany, error: insertError } = await supabase
    .from("companies")
    .insert(payload)
    .select("id")
    .single();

  if (insertError || !insertedCompany) {
    if (insertError?.message?.includes("companies_name_key")) {
      throw new Error(
        "Database still enforces unique company names. Run the latest schema update before syncing same-name companies."
      );
    }
    throw new Error(`Company insert failed: ${insertError?.message}`);
  }

  return insertedCompany;
}

// ── POST /api/sync — Receive all data from desktop connector ────

router.post("/", requireApiKey, async (req, res) => {
  const {
    company_name,
    company_guid,
    company_info,
    alter_ids,
    groups,
    ledgers,
    vouchers,
    stock_items,
    outstanding,
    profit_loss,
    balance_sheet,
    trial_balance,
    sync_meta,
  } = req.body;

  if (typeof company_name !== "string" || !company_name.trim()) {
    return res.status(400).json({ error: "company_name required" });
  }

  const normalizedCompanyName = company_name.trim();
  const normalizedCompanyGuid = normalizeTrimmedString(company_guid)
    || (company_info && typeof company_info === "object"
      ? normalizeTrimmedString((company_info as Record<string, unknown>).guid)
      : null);

  const validationError = [
    validateSectionArray("groups", groups),
    validateSectionArray("ledgers", ledgers),
    validateSectionArray("vouchers", vouchers),
    validateSectionArray("stock_items", stock_items),
    validateSectionArray("outstanding", outstanding),
    validateSectionArray("profit_loss", profit_loss),
    validateSectionArray("balance_sheet", balance_sheet),
    validateSectionArray("trial_balance", trial_balance),
  ].find(Boolean);

  if (validationError) {
    return res.status(400).json({ error: validationError });
  }

  let company_id: string | null = null;
  const syncedAt = new Date().toISOString();
  const normalizedSyncMeta = normalizeSyncMeta(sync_meta);
  const records = buildRecordCounts({
    groups,
    ledgers,
    vouchers,
    purchases: [],
    stock_items,
    outstanding,
    profit_loss,
    balance_sheet,
    trial_balance,
  });
  const companySyncState: Record<string, string> = {
    last_synced_at: syncedAt,
  };
  const snapshotCleanupTasks: Array<{
    table: keyof typeof SNAPSHOT_TIMESTAMP_COLUMNS;
    label: string;
    column: (typeof SNAPSHOT_TIMESTAMP_COLUMNS)[keyof typeof SNAPSHOT_TIMESTAMP_COLUMNS];
  }> = [];
  const snapshotInsertedRows: Array<{ table: string; label: string; insertedIds: string[] }> = [];
  let voucherRestoreContext: VoucherRestoreContext | null = null;
  let voucherGraphNeedsRestore = false;
  let companySyncStatePersisted = false;

  try {
    // 1. Upsert company
    const company = await upsertCompanyRecord(
      normalizedCompanyName,
      normalizedCompanyGuid,
      company_info,
      alter_ids,
    );
    const companyId = company.id;
    company_id = companyId;

    // 1.5 Upsert groups
    if (Array.isArray(groups)) {
      const rows = groups.map((g: any) => ({ ...g, company_id: companyId, synced_at: syncedAt }));
      await upsertInBatches("groups", rows, "company_id,name", "Groups");
    }

    // 2. Upsert ledgers
    if (Array.isArray(ledgers)) {
      const rows = ledgers.map((l: any) => ({ ...l, company_id: companyId, synced_at: syncedAt }));
      await upsertInBatches("ledgers", rows, "company_id,name", "Ledgers");
    }

    // 3. Upsert vouchers + items + ledger entries in batches
    if (Array.isArray(vouchers)) {
      const validVouchers = vouchers.filter((v: any) => v?.tally_guid);
      const purchaseVouchers = validVouchers.filter(
        (voucher: any) => isPurchaseVoucherType(voucher.voucher_type)
      );
      const voucherGuids = validVouchers
        .map((voucher: any) => voucher.tally_guid)
        .filter((value: unknown): value is string => typeof value === "string" && value.length > 0);
      const purchaseGuids = purchaseVouchers
        .map((voucher: any) => voucher.tally_guid)
        .filter((value: unknown): value is string => typeof value === "string" && value.length > 0);
      const voucherSnapshot = await captureVoucherGraphSnapshot(companyId, voucherGuids, purchaseGuids);
      voucherRestoreContext = {
        companyId,
        voucherGuids,
        snapshot: voucherSnapshot,
      };
      voucherGraphNeedsRestore = voucherGuids.length > 0
        || voucherSnapshot.voucherRows.length > 0
        || voucherSnapshot.itemRows.length > 0
        || voucherSnapshot.entryRows.length > 0
        || voucherSnapshot.purchaseRows.length > 0;

      try {
        const voucherRows = validVouchers.map(({ items, ledger_entries, ...vData }: any) => ({
          ...vData,
          company_id: companyId,
          synced_at: syncedAt,
        }));

        await upsertInBatches("vouchers", voucherRows, "company_id,tally_guid", "Vouchers");

        let purchaseRows: any[] = [];
        if (voucherRows.length) {
          const voucherIdMap = await getVoucherIdMap(
            companyId,
            voucherRows.map((voucher: any) => voucher.tally_guid)
          );
          const voucherIds = [...voucherIdMap.values()];
          const existingItemRows = await selectRowsByIn(
            "voucher_items",
            "voucher_id",
            voucherIds,
            "Voucher items"
          );
          const existingEntryRows = await selectRowsByIn(
            "voucher_ledger_entries",
            "voucher_id",
            voucherIds,
            "Voucher ledger entries"
          );
          const existingItemIds = existingItemRows
            .map((row: any) => row.id)
            .filter((id: unknown): id is string => typeof id === "string");
          const existingEntryIds = existingEntryRows
            .map((row: any) => row.id)
            .filter((id: unknown): id is string => typeof id === "string");

          const itemRows: any[] = [];
          const entryRows: any[] = [];

          for (const voucher of validVouchers) {
            const voucherId = voucherIdMap.get(voucher.tally_guid);
            if (!voucherId) {
              throw new Error(`Voucher id missing after upsert for ${voucher.tally_guid}`);
            }

            for (const item of voucher.items || []) {
              if (!item?.stock_item_name) continue;
              itemRows.push({ ...item, voucher_id: voucherId });
            }

            for (const entry of voucher.ledger_entries || []) {
              if (!entry?.ledger_name) continue;
              const { bill_allocations, ...entryData } = entry;
              entryRows.push({
                ...entryData,
                voucher_id: voucherId,
                bill_allocations: bill_allocations || [],
              });
            }
          }

          purchaseRows = purchaseVouchers
            .map((voucher: any) => ({
              company_id: companyId,
              voucher_id: voucherIdMap.get(voucher.tally_guid),
              tally_guid: voucher.tally_guid,
              voucher_number: voucher.voucher_number ?? null,
              voucher_type: voucher.voucher_type ?? null,
              date: voucher.date ?? null,
              party_name: voucher.party_name ?? null,
              amount: voucher.amount ?? 0,
              narration: voucher.narration ?? null,
              reference: voucher.reference ?? null,
              is_cancelled: voucher.is_cancelled ?? false,
              is_invoice: voucher.is_invoice ?? false,
              synced_at: syncedAt,
            }))
            .filter((purchase: any) => typeof purchase.voucher_id === "string");

          const itemInsert = await insertReturningIdsInBatches("voucher_items", itemRows, "Voucher items");
          if (itemInsert.error) {
            await rollbackInsertedRows("voucher_items", itemInsert.insertedIds, "Voucher items");
            throw itemInsert.error;
          }

          const entryInsert = await insertReturningIdsInBatches(
            "voucher_ledger_entries",
            entryRows,
            "Voucher ledger entries"
          );
          if (entryInsert.error) {
            await rollbackInsertedRows("voucher_ledger_entries", entryInsert.insertedIds, "Voucher ledger entries");
            await rollbackInsertedRows("voucher_items", itemInsert.insertedIds, "Voucher items");
            throw entryInsert.error;
          }

          try {
            await deleteByIn("voucher_items", "id", existingItemIds, "Voucher items previous rows");
            await deleteByIn(
              "voucher_ledger_entries",
              "id",
              existingEntryIds,
              "Voucher ledger entries previous rows"
            );
          } catch (error: any) {
            await rollbackInsertedRows("voucher_ledger_entries", entryInsert.insertedIds, "Voucher ledger entries");
            await rollbackInsertedRows("voucher_items", itemInsert.insertedIds, "Voucher items");
            await restoreRows("voucher_items", existingItemRows, "Voucher items");
            await restoreRows("voucher_ledger_entries", existingEntryRows, "Voucher ledger entries");
            throw error;
          }

          if (purchaseRows.length) {
            await upsertInBatches("purchases", purchaseRows, "company_id,tally_guid", "Purchases");
          }
        }

        await reconcileVoucherScope(
          companyId,
          validVouchers.map((voucher: any) => voucher.tally_guid),
          normalizedSyncMeta,
        );
        await reconcilePurchaseScope(
          companyId,
          purchaseVouchers.map((voucher: any) => voucher.tally_guid),
          normalizedSyncMeta,
        );
      } catch (error: any) {
        await restoreVoucherGraphSnapshot(companyId, voucherGuids, voucherSnapshot);
        voucherGraphNeedsRestore = false;
        throw error;
      }

      records.purchases = purchaseVouchers.length;
    }

    // 4. Upsert stock items
    if (Array.isArray(stock_items)) {
      const rows = stock_items.map((s: any) => ({ ...s, company_id: companyId, synced_at: syncedAt }));
      await upsertInBatches("stock_items", rows, "company_id,name", "Stock items");
    }

    // 5. Replace outstanding only when this section was provided
    if (Array.isArray(outstanding)) {
      const rows = outstanding.map((o: any) => ({ ...o, company_id: companyId, synced_at: syncedAt }));
      const insertResult = await insertReturningIdsInBatches("outstanding", rows, "Outstanding");
      if (insertResult.error) {
        await rollbackInsertedRows("outstanding", insertResult.insertedIds, "Outstanding");
        throw insertResult.error;
      }
      companySyncState[SNAPSHOT_TIMESTAMP_COLUMNS.outstanding] = syncedAt;
      snapshotInsertedRows.push({
        table: "outstanding",
        label: "Outstanding",
        insertedIds: insertResult.insertedIds,
      });
      snapshotCleanupTasks.push({
        table: "outstanding",
        label: "Outstanding",
        column: SNAPSHOT_TIMESTAMP_COLUMNS.outstanding,
      });
    }

    // 6. Replace profit & loss only when provided
    if (Array.isArray(profit_loss)) {
      const rows = profit_loss.map((p: any) => ({
        ...p,
        company_id: companyId,
        synced_at: syncedAt,
      }));
      const insertResult = await insertReturningIdsInBatches("profit_loss", rows, "Profit & Loss");
      if (insertResult.error) {
        await rollbackInsertedRows("profit_loss", insertResult.insertedIds, "Profit & Loss");
        throw insertResult.error;
      }
      companySyncState[SNAPSHOT_TIMESTAMP_COLUMNS.profit_loss] = syncedAt;
      snapshotInsertedRows.push({
        table: "profit_loss",
        label: "Profit & Loss",
        insertedIds: insertResult.insertedIds,
      });
      snapshotCleanupTasks.push({
        table: "profit_loss",
        label: "Profit & Loss",
        column: SNAPSHOT_TIMESTAMP_COLUMNS.profit_loss,
      });
    }

    // 7. Replace balance sheet only when provided
    if (Array.isArray(balance_sheet)) {
      const rows = balance_sheet.map((b: any) => ({
        ...b,
        company_id: companyId,
        synced_at: syncedAt,
      }));
      const insertResult = await insertReturningIdsInBatches("balance_sheet", rows, "Balance Sheet");
      if (insertResult.error) {
        await rollbackInsertedRows("balance_sheet", insertResult.insertedIds, "Balance Sheet");
        throw insertResult.error;
      }
      companySyncState[SNAPSHOT_TIMESTAMP_COLUMNS.balance_sheet] = syncedAt;
      snapshotInsertedRows.push({
        table: "balance_sheet",
        label: "Balance Sheet",
        insertedIds: insertResult.insertedIds,
      });
      snapshotCleanupTasks.push({
        table: "balance_sheet",
        label: "Balance Sheet",
        column: SNAPSHOT_TIMESTAMP_COLUMNS.balance_sheet,
      });
    }

    // 8. Replace trial balance only when provided
    if (Array.isArray(trial_balance)) {
      const rows = trial_balance.map((t: any) => ({
        ...t,
        company_id: companyId,
        synced_at: syncedAt,
      }));
      const insertResult = await insertReturningIdsInBatches("trial_balance", rows, "Trial Balance");
      if (insertResult.error) {
        await rollbackInsertedRows("trial_balance", insertResult.insertedIds, "Trial Balance");
        throw insertResult.error;
      }
      companySyncState[SNAPSHOT_TIMESTAMP_COLUMNS.trial_balance] = syncedAt;
      snapshotInsertedRows.push({
        table: "trial_balance",
        label: "Trial Balance",
        insertedIds: insertResult.insertedIds,
      });
      snapshotCleanupTasks.push({
        table: "trial_balance",
        label: "Trial Balance",
        column: SNAPSHOT_TIMESTAMP_COLUMNS.trial_balance,
      });
    }

    const { data: updatedCompany, error: companySyncStateError } = await supabase
      .from("companies")
      .update(companySyncState)
      .eq("id", companyId)
      .select(`
        last_outstanding_synced_at,
        last_profit_loss_synced_at,
        last_balance_sheet_synced_at,
        last_trial_balance_synced_at
      `)
      .single();

    if (companySyncStateError) {
      throw new Error(`Company sync state update failed: ${companySyncStateError.message}`);
    }
    companySyncStatePersisted = true;
    voucherGraphNeedsRestore = false;

    for (const snapshot of snapshotCleanupTasks) {
      const activeSyncedAt = updatedCompany?.[snapshot.column];
      if (typeof activeSyncedAt === "string" && activeSyncedAt) {
        await cleanupSnapshotRows(snapshot.table, companyId, activeSyncedAt, snapshot.label);
      }
    }

    // 9. Log the sync (best-effort only; don't fail a good data write on log issues)
    let syncLogError: { message: string } | null = null;
    const syncLogWithMeta = await supabase.from("sync_log").insert({
      company_id: companyId,
      status: "success",
      records_synced: records,
      sync_meta: normalizedSyncMeta,
    });
    syncLogError = syncLogWithMeta.error;

    if (syncLogError?.message?.toLowerCase().includes("sync_meta")) {
      const fallbackSyncLog = await supabase.from("sync_log").insert({
        company_id: companyId,
        status: "success",
        records_synced: records,
      });
      syncLogError = fallbackSyncLog.error;
    }

    if (syncLogError) {
      console.warn("[Sync] Sync log warning:", syncLogError.message);
    }

    res.json({
      success: true,
      company_id,
      records,
    });
  } catch (err: any) {
    if (!companySyncStatePersisted) {
      if (voucherGraphNeedsRestore && voucherRestoreContext) {
        await restoreVoucherGraphSnapshot(
          voucherRestoreContext.companyId,
          voucherRestoreContext.voucherGuids,
          voucherRestoreContext.snapshot,
        );
        voucherGraphNeedsRestore = false;
      }

      for (const snapshot of snapshotInsertedRows) {
        await rollbackInsertedRows(snapshot.table, snapshot.insertedIds, snapshot.label);
      }
    }

    const errorMessage = withSupabaseSchemaGuidance(err.message || "Unknown sync error");
    console.error("[Sync] Error:", errorMessage);
    res.status(500).json({ error: errorMessage });
  }
});

// PUSH PHASE 1: queue outbound Sales/Purchase voucher imports without touching
// the current inbound sync route or renderer flow.
router.post("/push-queue", requireApiKey, async (req, res) => {
  const { company_id, company_guid, company_name, voucher_payload } = req.body || {};

  const companyLookup = await resolveCompanyLookup({
    companyId: company_id,
    companyGuid: company_guid,
    companyName: company_name,
  }, { requireSuccessfulSync: false });
  if (companyLookup.status !== 200) {
    return res.status(companyLookup.status).json({ error: companyLookup.error });
  }

  const normalizedVoucher = normalizePushVoucherPayload(voucher_payload);
  if (normalizedVoucher.error) {
    return res.status(400).json({ error: normalizedVoucher.error });
  }

  try {
    const { data, error } = await supabase
      .from("push_queue")
      .insert({
        company_id: companyLookup.companyId,
        voucher_payload: normalizedVoucher.voucher,
        status: "pending",
      })
      .select("id, status, created_at")
      .single();

    if (error || !data) {
      throw new Error(`Push queue insert failed: ${error?.message}`);
    }

    return res.json({
      success: true,
      job: data,
    });
  } catch (err: any) {
    const errorMessage = withSupabaseSchemaGuidance(err.message || "Could not enqueue push voucher");
    console.error("[PushQueue] Enqueue error:", errorMessage);
    return res.status(500).json({ error: errorMessage });
  }
});

router.get("/push-queue", requireApiKey, async (req, res) => {
  const companyLookup = await resolveCompanyLookup({
    companyId: req.query.company_id,
    companyGuid: req.query.company_guid,
    companyName: req.query.company_name,
  }, { requireSuccessfulSync: false });
  if (companyLookup.status !== 200) {
    return res.status(companyLookup.status).json({ error: companyLookup.error });
  }

  const requestedLimit = normalizeFiniteNumber(req.query.limit);
  const limit = requestedLimit == null
    ? DEFAULT_PUSH_QUEUE_BATCH_SIZE
    : Math.max(1, Math.min(MAX_PUSH_QUEUE_BATCH_SIZE, Math.trunc(requestedLimit)));

  try {
    const { data, error } = await supabase
      .from("push_queue")
      .select("id, voucher_payload, created_at")
      .eq("company_id", companyLookup.companyId)
      .eq("status", "pending")
      .order("created_at", { ascending: true })
      .limit(limit);

    if (error) {
      throw new Error(`Push queue lookup failed: ${error.message}`);
    }

    const jobs = (data || []).filter((row: any) =>
      isAllowedPushVoucherType(row?.voucher_payload?.voucher_type)
    );
    return res.json({ jobs });
  } catch (err: any) {
    const errorMessage = withSupabaseSchemaGuidance(err.message || "Could not fetch push queue");
    console.error("[PushQueue] Lookup error:", errorMessage);
    return res.status(500).json({ error: errorMessage });
  }
});

router.post("/push-results", requireApiKey, async (req, res) => {
  const rawResults = Array.isArray(req.body) ? req.body : req.body?.results;
  if (!Array.isArray(rawResults) || !rawResults.length) {
    return res.status(400).json({ error: "results must be a non-empty array" });
  }

  try {
    for (const [index, result] of rawResults.entries()) {
      const rawResult = result && typeof result === "object"
        ? (result as Record<string, unknown>)
        : null;
      if (!rawResult) {
        return res.status(400).json({ error: `results[${index}] must be an object` });
      }

      const id = normalizeTrimmedString(rawResult.id);
      if (!id) {
        return res.status(400).json({ error: `results[${index}].id is required` });
      }

      const status = normalizeTrimmedString(rawResult.status);
      if (status !== "pushed" && status !== "failed") {
        return res.status(400).json({ error: `results[${index}].status must be pushed or failed` });
      }

      const updatePayload: Record<string, unknown> = {
        status,
        error_message: normalizeTrimmedString(rawResult.error_message),
        tally_response: rawResult.tally_response ?? null,
        pushed_at: status === "pushed" ? new Date().toISOString() : null,
      };

      const { error } = await supabase
        .from("push_queue")
        .update(updatePayload)
        .eq("id", id);

      if (error) {
        throw new Error(`Push result update failed for ${id}: ${error.message}`);
      }
    }

    return res.json({ success: true, updated: rawResults.length });
  } catch (err: any) {
    const errorMessage = withSupabaseSchemaGuidance(err.message || "Could not store push results");
    console.error("[PushQueue] Result error:", errorMessage);
    return res.status(500).json({ error: errorMessage });
  }
});


// ── Pagination helper ────────────────────────────────────────────
// Supabase defaults to 1000 rows per query. This helper auto-paginates
// until all rows are retrieved.
async function fetchAllPages<T = any>(
  label: string,
  buildQuery: (from: number, to: number) => PromiseLike<{ data: T[] | null; error: any }>
): Promise<T[]> {
  const PAGE_SIZE = 1000;
  const result: T[] = [];
  let offset = 0;
  while (true) {
    const { data, error } = await buildQuery(offset, offset + PAGE_SIZE - 1);
    if (error) {
      throw new Error(
        `${label} query failed for rows ${offset}-${offset + PAGE_SIZE - 1}: ${error.message}`
      );
    }
    if (!data || data.length === 0) break;
    result.push(...data);
    if (data.length < PAGE_SIZE) break;
    offset += PAGE_SIZE;
  }
  return result;
}

// ── GET /api/sync/party-ledger — Individual customer/party history ──
// Derives party transaction history from already-synced vouchers.
// Query params: company_id or company_guid or company_name, plus party_name

router.get("/party-ledger", requireApiKey, async (req, res) => {
  const { company_id, company_guid, company_name, party_name } = req.query;

  if (!party_name) {
    return res.status(400).json({
      error: "party_name query param required",
    });
  }

  try {
    const companyLookup = await resolveCompanyLookup({
      companyId: company_id,
      companyGuid: company_guid,
      companyName: company_name,
    });
    if (companyLookup.status !== 200) {
      return res.status(companyLookup.status).json({ error: companyLookup.error });
    }

    // 2. Get all vouchers for this party
    const vouchers = await fetchAllPages("Party vouchers", (from, to) =>
      supabase
        .from("vouchers")
        .select("*, voucher_items(*), voucher_ledger_entries(*)")
        .eq("company_id", companyLookup.companyId)
        .eq("party_name", party_name)
        .order("date", { ascending: true })
        .order("id", { ascending: true })
        .range(from, to)
    );

    // 3. Get outstanding for this party
    let outstanding: any[] = [];
    if (companyLookup.lastOutstandingSyncedAt) {
      const { data, error: oErr } = await supabase
        .from("outstanding")
        .select("*")
        .eq("company_id", companyLookup.companyId)
        .eq("synced_at", companyLookup.lastOutstandingSyncedAt)
        .eq("party_name", party_name);

      if (oErr) {
        throw new Error(`Outstanding query failed: ${oErr.message}`);
      }

      outstanding = data || [];
    }

    // 4. Get ledger info for this party
    const { data: ledger, error: lErr } = await supabase
      .from("ledgers")
      .select("*")
      .eq("company_id", companyLookup.companyId)
      .eq("name", party_name)
      .single();

    // 5. Build running balance
    let runningBalance = 0;
    const transactions = (vouchers || []).map((v: any) => {
      const partyEntry = (v.voucher_ledger_entries || []).find((entry: any) => entry.is_party_ledger);
      let signedAmount = 0;

      if (partyEntry) {
        const amount = Math.abs(partyEntry.amount || 0);
        signedAmount = partyEntry.is_deemed_positive ? amount : -amount;
      } else {
        const amount = Math.abs(v.amount || 0);
        const isDebit = ["Sales", "Receipt", "Debit Note"].includes(v.voucher_type);
        signedAmount = isDebit ? amount : -amount;
      }

      runningBalance += signedAmount;

      return {
        date: v.date,
        voucher_type: v.voucher_type,
        voucher_number: v.voucher_number,
        amount: v.amount,
        narration: v.narration,
        items: v.voucher_items || [],
        running_balance: runningBalance,
      };
    });

    res.json({
      party_name,
      company_name: companyLookup.companyName,
      company_guid: companyLookup.companyGuid,
      ledger: ledger || null,
      outstanding_summary: {
        total_outstanding: outstanding?.reduce(
          (sum: number, o: any) => sum + Math.abs(o.pending_amount || 0),
          0
        ) || 0,
        bills: outstanding || [],
      },
      transactions,
      total_transactions: transactions.length,
    });
  } catch (err: any) {
    console.error("[PartyLedger] Error:", err.message);
    res.status(500).json({ error: err.message });
  }
});

// ── GET routes for web dashboard ─────────────────────────────────

router.get("/vouchers", requireApiKey, async (req, res) => {
  const companyLookup = await resolveCompanyLookup({
    companyId: req.query.company_id,
    companyGuid: req.query.company_guid,
    companyName: req.query.company_name,
  });
  if (companyLookup.status !== 200) {
    return res.status(companyLookup.status).json({ error: companyLookup.error });
  }
  const data = await fetchAllPages("Vouchers", (from, to) =>
    supabase.from("vouchers").select("*, voucher_items(*)")
      .eq("company_id", companyLookup.companyId)
      .order("date", { ascending: false })
      .order("id", { ascending: false })
      .range(from, to)
  );
  res.json({ vouchers: data });
});

router.get("/outstanding", requireApiKey, async (req, res) => {
  const companyLookup = await resolveCompanyLookup({
    companyId: req.query.company_id,
    companyGuid: req.query.company_guid,
    companyName: req.query.company_name,
  });
  if (companyLookup.status !== 200) {
    return res.status(companyLookup.status).json({ error: companyLookup.error });
  }
  if (!companyLookup.lastOutstandingSyncedAt) {
    return res.json({ outstanding: [] });
  }
  const data = await fetchAllPages("Outstanding", (from, to) =>
    supabase.from("outstanding").select("*")
      .eq("company_id", companyLookup.companyId)
      .eq("synced_at", companyLookup.lastOutstandingSyncedAt)
      .order("days_overdue", { ascending: false })
      .order("id", { ascending: true })
      .range(from, to)
  );
  res.json({ outstanding: data });
});

router.get("/stock", requireApiKey, async (req, res) => {
  const companyLookup = await resolveCompanyLookup({
    companyId: req.query.company_id,
    companyGuid: req.query.company_guid,
    companyName: req.query.company_name,
  });
  if (companyLookup.status !== 200) {
    return res.status(companyLookup.status).json({ error: companyLookup.error });
  }
  const data = await fetchAllPages("Stock items", (from, to) =>
    supabase.from("stock_items").select("*")
      .eq("company_id", companyLookup.companyId)
      .order("id", { ascending: true })
      .range(from, to)
  );
  res.json({ stock_items: data });
});

router.get("/purchases", requireApiKey, async (req, res) => {
  const companyLookup = await resolveCompanyLookup({
    companyId: req.query.company_id,
    companyGuid: req.query.company_guid,
    companyName: req.query.company_name,
  });
  if (companyLookup.status !== 200) {
    return res.status(companyLookup.status).json({ error: companyLookup.error });
  }
  const data = await fetchAllPages("Purchases", (from, to) =>
    supabase.from("purchases").select("*")
      .eq("company_id", companyLookup.companyId)
      .order("date", { ascending: false })
      .order("id", { ascending: false })
      .range(from, to)
  );
  res.json({ purchases: data });
});

router.get("/pnl", requireApiKey, async (req, res) => {
  const companyLookup = await resolveCompanyLookup({
    companyId: req.query.company_id,
    companyGuid: req.query.company_guid,
    companyName: req.query.company_name,
  });
  if (companyLookup.status !== 200) {
    return res.status(companyLookup.status).json({ error: companyLookup.error });
  }
  if (!companyLookup.lastProfitLossSyncedAt) {
    return res.json({ profit_loss: [] });
  }
  const { data } = await supabase
    .from("profit_loss")
    .select("*")
    .eq("company_id", companyLookup.companyId)
    .eq("synced_at", companyLookup.lastProfitLossSyncedAt);
  res.json({ profit_loss: data || [] });
});

router.get("/balance-sheet", requireApiKey, async (req, res) => {
  const companyLookup = await resolveCompanyLookup({
    companyId: req.query.company_id,
    companyGuid: req.query.company_guid,
    companyName: req.query.company_name,
  });
  if (companyLookup.status !== 200) {
    return res.status(companyLookup.status).json({ error: companyLookup.error });
  }
  if (!companyLookup.lastBalanceSheetSyncedAt) {
    return res.json({ balance_sheet: [] });
  }
  const { data } = await supabase
    .from("balance_sheet")
    .select("*")
    .eq("company_id", companyLookup.companyId)
    .eq("synced_at", companyLookup.lastBalanceSheetSyncedAt);
  res.json({ balance_sheet: data || [] });
});

router.get("/trial-balance", requireApiKey, async (req, res) => {
  const companyLookup = await resolveCompanyLookup({
    companyId: req.query.company_id,
    companyGuid: req.query.company_guid,
    companyName: req.query.company_name,
  });
  if (companyLookup.status !== 200) {
    return res.status(companyLookup.status).json({ error: companyLookup.error });
  }
  if (!companyLookup.lastTrialBalanceSyncedAt) {
    return res.json({ trial_balance: [] });
  }
  const { data } = await supabase
    .from("trial_balance")
    .select("*")
    .eq("company_id", companyLookup.companyId)
    .eq("synced_at", companyLookup.lastTrialBalanceSyncedAt);
  res.json({ trial_balance: data || [] });
});


router.get("/alter-ids", requireApiKey, async (req, res) => {
  const companyLookup = await resolveCompanyLookup({
    companyId: req.query.company_id,
    companyGuid: req.query.company_guid,
    companyName: req.query.company_name,
  }, { requireSuccessfulSync: false });
  if (companyLookup.status !== 200) {
    return res.status(companyLookup.status).json({ error: companyLookup.error });
  }

  const { data } = await supabase
    .from("companies")
    .select(
      "alter_id, alt_vch_id, alt_mst_id, last_synced_at, "
      + "last_outstanding_synced_at, last_profit_loss_synced_at, "
      + "last_balance_sheet_synced_at, last_trial_balance_synced_at"
    )
    .eq("id", companyLookup.companyId)
    .single();
  res.json(data || {});
});


// ── GET /api/sync/parties — List all parties for a company ──────
// Returns all unique party names from vouchers with summary stats.

router.get("/parties", requireApiKey, async (req, res) => {
  try {
    const companyLookup = await resolveCompanyLookup({
      companyId: req.query.company_id,
      companyGuid: req.query.company_guid,
      companyName: req.query.company_name,
    });
    if (companyLookup.status !== 200) {
      return res.status(companyLookup.status).json({ error: companyLookup.error });
    }

    // Get all parties from ledgers (Sundry Debtors + Sundry Creditors)
    const { data: parties, error: pErr } = await supabase
      .from("ledgers")
      .select("name, group_name, opening_balance, closing_balance")
      .eq("company_id", companyLookup.companyId)
      .in("group_name", ["Sundry Debtors", "Sundry Creditors"]);

    if (pErr) {
      throw new Error(`Parties query failed: ${pErr.message}`);
    }

    // Get outstanding totals per party
    let outstandingList: any[] = [];
    if (companyLookup.lastOutstandingSyncedAt) {
      outstandingList = await fetchAllPages("Outstanding party totals", (from, to) =>
        supabase.from("outstanding")
          .select("party_name, type, pending_amount")
          .eq("company_id", companyLookup.companyId)
          .eq("synced_at", companyLookup.lastOutstandingSyncedAt)
          .order("party_name", { ascending: true })
          .order("id", { ascending: true })
          .range(from, to)
      );
    }

    // Build party summary
    const outstandingMap: Record<string, number> = {};
    for (const o of outstandingList || []) {
      const key = o.party_name;
      outstandingMap[key] = (outstandingMap[key] || 0) + Math.abs(o.pending_amount || 0);
    }

    const result = (parties || []).map((p: any) => ({
      name: p.name,
      group: p.group_name,
      type: p.group_name === "Sundry Debtors" ? "customer" : "supplier",
      closing_balance: p.closing_balance,
      total_outstanding: outstandingMap[p.name] || 0,
    }));

    res.json({ parties: result, total: result.length });
  } catch (err: any) {
    console.error("[Parties] Error:", err.message);
    res.status(500).json({ error: err.message });
  }
});

// ── GET /api/sync/reorder-levels ────────────────────────────────────────────
// Auto-detects the single company in Supabase — no company param required.

// Returns the direct amount-based inventory intelligence report.
router.get("/reorder-levels", requireApiKey, async (req, res) => {
  try {
    const format = parseInventoryResponseFormat(req.query.format);
    const companyLookup = await resolveCompanyLookup({
      companyId: req.query.company_id,
      companyGuid: req.query.company_guid,
      companyName: req.query.company_name,
    });
    if (companyLookup.status !== 200) {
      return res.status(companyLookup.status).json({ error: companyLookup.error });
    }

    const report = await buildInventoryIntelligenceReport({
      companyId: companyLookup.companyId,
      companyGuid: companyLookup.companyGuid,
      companyName: companyLookup.companyName,
      asOfDateInput: req.query.as_of_date,
      thresholdInput: req.query.threshold,
      limitInput: req.query.limit,
    });

    return sendInventoryIntelligenceResponse(res, report, format);


    // Parse as_of_date — default 2019-03-31 (FY end for K.V. ENTERPRISES 18-19)



  } catch (err: any) {
    console.error("[ReorderLevels] Error:", err.message);
    res.status(500).json({ error: err.message });
  }
});

router.get("/reorder-levels/:reportKey", requireApiKey, async (req, res) => {
  try {
    const format = parseInventoryResponseFormat(req.query.format);
    const reportKey = normalizeInventoryReportKey(req.params.reportKey);
    if (!reportKey) {
      return res.status(400).json({
        error: `reportKey must be one of: ${INVENTORY_REPORT_KEYS.map(
          (key) => `${INVENTORY_REPORT_META[key].id}/${key}`,
        ).join(", ")}`,
      });
    }

    const companyLookup = await resolveCompanyLookup({
      companyId: req.query.company_id,
      companyGuid: req.query.company_guid,
      companyName: req.query.company_name,
    });
    if (companyLookup.status !== 200) {
      return res.status(companyLookup.status).json({ error: companyLookup.error });
    }

    const report = await buildInventoryIntelligenceReport({
      companyId: companyLookup.companyId,
      companyGuid: companyLookup.companyGuid,
      companyName: companyLookup.companyName,
      asOfDateInput: req.query.as_of_date,
      thresholdInput: req.query.threshold,
      limitInput: req.query.limit,
      reportKeyFilter: reportKey,
    });

    return sendInventoryIntelligenceResponse(res, report, format);
  } catch (err: any) {
    console.error("[ReorderLevels] Report Filter Error:", err.message);
    res.status(500).json({ error: err.message });
  }
});

export default router;

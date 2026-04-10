import { Router } from "express";
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

    console.error("[Sync] Error:", err.message);
    res.status(500).json({ error: err.message });
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
// Returns 90-day purchase-volume reorder trigger for all stock items.
// Auto-detects the single company in Supabase — no company param required.
// Formula: reorder_trigger = SUM(purchase qty in last 90 days), no averaging.
// Sorted: needs_reorder=true first, then alphabetical.
//
// Optional query param:
//   as_of_date  YYYY-MM-DD  end of 90-day window (default: 2019-03-31)

router.get("/reorder-levels", requireApiKey, async (req, res) => {
  try {
    // Auto-detect the single company
    const { data: companyRow, error: companyErr } = await supabase
      .from("companies")
      .select("id")
      .limit(1)
      .single();

    if (companyErr || !companyRow) {
      return res.status(404).json({ error: "No company found in database" });
    }

    const companyId: string = companyRow.id;

    // Parse as_of_date — default 2019-03-31 (FY end for K.V. ENTERPRISES 18-19)
    const DEFAULT_AS_OF = "2019-03-31";
    const rawDate = String(req.query.as_of_date ?? "").trim();
    const asOfDate = /^\d{4}-\d{2}-\d{2}$/.test(rawDate) ? rawDate : DEFAULT_AS_OF;

    // Compute inclusive 90-day window: subtract 89 days so both ends count
    const toDate = new Date(asOfDate);
    const fromDate = new Date(asOfDate);
    fromDate.setDate(toDate.getDate() - 89);
    const fromIso = fromDate.toISOString().slice(0, 10);
    const toIso = toDate.toISOString().slice(0, 10);

    // Step 1: fetch purchase voucher_ids in window (non-cancelled only)
    const purchaseRows = await fetchAllPages("Reorder purchases", (from, to) =>
      supabase
        .from("purchases")
        .select("voucher_id")
        .eq("company_id", companyId)
        .eq("is_cancelled", false)
        .gte("date", fromIso)
        .lte("date", toIso)
        .order("id", { ascending: true })
        .range(from, to)
    );

    const voucherIds = [
      ...new Set(purchaseRows.map((p: any) => p.voucher_id).filter(Boolean)),
    ];

    // Step 2: fetch voucher_items and aggregate qty per stock item name
    const qtyByItem = new Map<string, number>();
    if (voucherIds.length > 0) {
      const lineItems = await selectRowsByIn(
        "voucher_items",
        "voucher_id",
        voucherIds,
        "Reorder voucher items"
      );
      for (const item of lineItems) {
        const name: string = item.stock_item_name;
        if (!name || !name.trim()) continue;          // skip blank names
        const qty = Number(item.quantity) || 0;
        if (qty <= 0) continue;                       // skip returns / zero-qty rows
        qtyByItem.set(name, (qtyByItem.get(name) ?? 0) + qty);
      }
    }

    // Step 3: fetch all stock items for the company
    const stockItems = await fetchAllPages("Reorder stock items", (from, to) =>
      supabase
        .from("stock_items")
        .select("name, unit, closing_qty")
        .eq("company_id", companyId)
        .order("name", { ascending: true })
        .range(from, to)
    );

    // Step 4: merge — reorder_trigger = raw 90-day purchase total
    const result = stockItems.map((s: any) => {
      const totalQtyPurchased = qtyByItem.get(s.name) ?? 0;
      const reorderTrigger = totalQtyPurchased;
      const closingQty = Number(s.closing_qty) || 0;
      return {
        stock_item_name: s.name,
        unit: s.unit ?? null,
        total_qty_purchased: totalQtyPurchased,
        reorder_trigger: reorderTrigger,
        closing_qty: closingQty,
        needs_reorder: reorderTrigger > 0 && closingQty <= reorderTrigger,
      };
    });

    // Sort: needs_reorder first, then alphabetical
    result.sort((a: any, b: any) => {
      if (a.needs_reorder !== b.needs_reorder) return a.needs_reorder ? -1 : 1;
      return a.stock_item_name.localeCompare(b.stock_item_name);
    });

    res.json({
      as_of_date: asOfDate,
      window_from: fromIso,
      window_to: toIso,
      total_items: result.length,
      needs_reorder_count: result.filter((r: any) => r.needs_reorder).length,
      items: result,
    });
  } catch (err: any) {
    console.error("[ReorderLevels] Error:", err.message);
    res.status(500).json({ error: err.message });
  }
});

export default router;

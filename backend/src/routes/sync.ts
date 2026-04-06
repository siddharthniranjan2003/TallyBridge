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
  master_changed: boolean;
  voucher_changed: boolean;
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
    master_changed: raw.master_changed !== false,
    voucher_changed: raw.voucher_changed !== false,
  };
}

function buildCompanyUpsertPayload(
  companyName: string,
  companyInfo: unknown,
  alterIds: unknown,
) {
  const payload: Record<string, unknown> = { name: companyName };

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

async function resolveCompanyIdByName(companyName: unknown) {
  if (typeof companyName !== "string" || !companyName.trim()) {
    return { status: 400 as const, error: "company_name query param required" };
  }

  const { data: company, error } = await supabase
    .from("companies")
    .select(`
      id,
      last_synced_at,
      last_outstanding_synced_at,
      last_profit_loss_synced_at,
      last_balance_sheet_synced_at,
      last_trial_balance_synced_at
    `)
    .eq("name", companyName.trim())
    .single();

  if (error || !company) {
    return { status: 404 as const, error: "Company not found" };
  }

  if (!company.last_synced_at) {
    return { status: 409 as const, error: "Company has not completed a successful sync yet" };
  }

  return {
    status: 200 as const,
    companyId: company.id,
    lastSyncedAt: company.last_synced_at ?? null,
    lastOutstandingSyncedAt: company.last_outstanding_synced_at ?? null,
    lastProfitLossSyncedAt: company.last_profit_loss_synced_at ?? null,
    lastBalanceSheetSyncedAt: company.last_balance_sheet_synced_at ?? null,
    lastTrialBalanceSyncedAt: company.last_trial_balance_synced_at ?? null,
  };
}

// ── POST /api/sync — Receive all data from desktop connector ────

router.post("/", requireApiKey, async (req, res) => {
  const {
    company_name,
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

  try {
    // 1. Upsert company
    const { data: company, error: companyErr } = await supabase
      .from("companies")
      .upsert(
        buildCompanyUpsertPayload(company_name, company_info, alter_ids),
        { onConflict: "name" }
      )
      .select("id")
      .single();

    if (companyErr || !company) {
      throw new Error(`Company upsert failed: ${companyErr?.message}`);
    }
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
      const voucherRows = validVouchers.map(({ items, ledger_entries, ...vData }: any) => ({
        ...vData,
        company_id: companyId,
        synced_at: syncedAt,
      }));

      await upsertInBatches("vouchers", voucherRows, "company_id,tally_guid", "Vouchers");

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
      }

      await reconcileVoucherScope(
        companyId,
        validVouchers.map((voucher: any) => voucher.tally_guid),
        normalizedSyncMeta,
      );
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
      for (const snapshot of snapshotInsertedRows) {
        await rollbackInsertedRows(snapshot.table, snapshot.insertedIds, snapshot.label);
      }
      throw new Error(`Company sync state update failed: ${companySyncStateError.message}`);
    }

    for (const snapshot of snapshotCleanupTasks) {
      const activeSyncedAt = updatedCompany?.[snapshot.column];
      if (typeof activeSyncedAt === "string" && activeSyncedAt) {
        await cleanupSnapshotRows(snapshot.table, companyId, activeSyncedAt, snapshot.label);
      }
    }

    // 9. Log the sync (best-effort only; don't fail a good data write on log issues)
    const { error: syncLogError } = await supabase.from("sync_log").insert({
      company_id: companyId,
      status: "success",
      records_synced: records,
    });
    if (syncLogError) {
      console.warn("[Sync] Sync log warning:", syncLogError.message);
    }

    res.json({
      success: true,
      company_id,
      records,
    });
  } catch (err: any) {
    console.error("[Sync] Error:", err.message);
    res.status(500).json({ error: err.message });
  }
});


// ── GET /api/sync/party-ledger — Individual customer/party history ──
// Derives party transaction history from already-synced vouchers.
// Query params: company_name (required), party_name (required)

router.get("/party-ledger", requireApiKey, async (req, res) => {
  const { company_name, party_name } = req.query;

  if (!company_name || !party_name) {
    return res.status(400).json({
      error: "company_name and party_name query params required",
    });
  }

  try {
    const companyLookup = await resolveCompanyIdByName(company_name);
    if (companyLookup.status !== 200) {
      return res.status(companyLookup.status).json({ error: companyLookup.error });
    }

    // 2. Get all vouchers for this party
    const { data: vouchers, error: vErr } = await supabase
      .from("vouchers")
      .select("*, voucher_items(*), voucher_ledger_entries(*)")
      .eq("company_id", companyLookup.companyId)
      .eq("party_name", party_name)
      .order("date", { ascending: true });

    if (vErr) {
      throw new Error(`Voucher query failed: ${vErr.message}`);
    }

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
      company_name,
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
  const companyLookup = await resolveCompanyIdByName(req.query.company_name);
  if (companyLookup.status !== 200) {
    return res.status(companyLookup.status).json({ error: companyLookup.error });
  }
  const { data } = await supabase
    .from("vouchers").select("*, voucher_items(*)")
    .eq("company_id", companyLookup.companyId)
    .order("date", { ascending: false });
  res.json({ vouchers: data || [] });
});

router.get("/outstanding", requireApiKey, async (req, res) => {
  const companyLookup = await resolveCompanyIdByName(req.query.company_name);
  if (companyLookup.status !== 200) {
    return res.status(companyLookup.status).json({ error: companyLookup.error });
  }
  if (!companyLookup.lastOutstandingSyncedAt) {
    return res.json({ outstanding: [] });
  }
  const { data } = await supabase
    .from("outstanding").select("*")
    .eq("company_id", companyLookup.companyId)
    .eq("synced_at", companyLookup.lastOutstandingSyncedAt)
    .order("days_overdue", { ascending: false });
  res.json({ outstanding: data || [] });
});

router.get("/stock", requireApiKey, async (req, res) => {
  const companyLookup = await resolveCompanyIdByName(req.query.company_name);
  if (companyLookup.status !== 200) {
    return res.status(companyLookup.status).json({ error: companyLookup.error });
  }
  const { data } = await supabase
    .from("stock_items").select("*").eq("company_id", companyLookup.companyId);
  res.json({ stock_items: data || [] });
});

router.get("/pnl", requireApiKey, async (req, res) => {
  const companyLookup = await resolveCompanyIdByName(req.query.company_name);
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
  const companyLookup = await resolveCompanyIdByName(req.query.company_name);
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


router.get("/alter-ids", requireApiKey, async (req, res) => {
  if (typeof req.query.company_name !== "string" || !req.query.company_name.trim()) {
    return res.status(400).json({ error: "company_name query param required" });
  }
  const { data } = await supabase
    .from("companies")
    .select("alter_id, alt_vch_id, alt_mst_id")
    .eq("name", req.query.company_name.trim())
    .single();
  res.json(data || {});
});


// ── GET /api/sync/parties — List all parties for a company ──────
// Returns all unique party names from vouchers with summary stats.

router.get("/parties", requireApiKey, async (req, res) => {
  const { company_name } = req.query;

  if (!company_name) {
    return res.status(400).json({ error: "company_name query param required" });
  }

  try {
    const companyLookup = await resolveCompanyIdByName(company_name);
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
      const { data, error: oErr } = await supabase
        .from("outstanding")
        .select("party_name, type, pending_amount")
        .eq("company_id", companyLookup.companyId)
        .eq("synced_at", companyLookup.lastOutstandingSyncedAt);

      if (oErr) {
        throw new Error(`Outstanding query failed: ${oErr.message}`);
      }

      outstandingList = data || [];
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

export default router;

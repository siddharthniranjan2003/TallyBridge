import { Router } from "express";
import { supabase } from "../db/supabase.js";
import { requireApiKey } from "../middleware/auth.js";

const router = Router();
const BATCH_SIZE = 250;

function chunkArray<T>(items: T[], size = BATCH_SIZE): T[][] {
  const chunks: T[][] = [];
  for (let index = 0; index < items.length; index += size) {
    chunks.push(items.slice(index, index + size));
  }
  return chunks;
}

function buildRecordCounts(sections: Record<string, unknown>) {
  const records: Record<string, number> = {};
  for (const [key, value] of Object.entries(sections)) {
    if (Array.isArray(value)) {
      records[key] = value.length;
    }
  }
  return records;
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

// ── POST /api/sync — Receive all data from desktop connector ────

router.post("/", requireApiKey, async (req, res) => {
  const {
    company_name,
    company_info,
    groups,
    ledgers,
    vouchers,
    stock_items,
    outstanding,
    profit_loss,
    balance_sheet,
    trial_balance,
  } = req.body;

  if (!company_name) {
    return res.status(400).json({ error: "company_name required" });
  }

  let company_id: string | null = null;
  const syncedAt = new Date().toISOString();
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

  try {
    // 1. Upsert company
    const { data: company, error: companyErr } = await supabase
      .from("companies")
      .upsert(
        { 
          name: company_name, 
          last_synced_at: syncedAt,
          ...(company_info?.books_from ? { books_from: company_info.books_from } : {}),
          ...(company_info?.books_to ? { books_to: company_info.books_to } : {}),
          ...(company_info?.gstin ? { gstin: company_info.gstin } : {}),
          ...(company_info?.address ? { address: company_info.address } : {}),
        },
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

        await deleteByIn("voucher_items", "voucher_id", voucherIds, "Voucher items");
        await deleteByIn("voucher_ledger_entries", "voucher_id", voucherIds, "Voucher ledger entries");

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

        await insertInBatches("voucher_items", itemRows, "Voucher items");
        await insertInBatches("voucher_ledger_entries", entryRows, "Voucher ledger entries");
      }
    }

    // 4. Upsert stock items
    if (Array.isArray(stock_items)) {
      const rows = stock_items.map((s: any) => ({ ...s, company_id: companyId, synced_at: syncedAt }));
      await upsertInBatches("stock_items", rows, "company_id,name", "Stock items");
    }

    // 5. Replace outstanding only when this section was provided
    if (Array.isArray(outstanding)) {
      await deleteByEq("outstanding", "company_id", companyId, "Outstanding");
      const rows = outstanding.map((o: any) => ({ ...o, company_id: companyId, synced_at: syncedAt }));
      await insertInBatches("outstanding", rows, "Outstanding");
    }

    // 6. Replace profit & loss only when provided
    if (Array.isArray(profit_loss)) {
      await deleteByEq("profit_loss", "company_id", companyId, "Profit & Loss");
      const rows = profit_loss.map((p: any) => ({
        ...p,
        company_id: companyId,
        synced_at: syncedAt,
      }));
      await insertInBatches("profit_loss", rows, "Profit & Loss");
    }

    // 7. Replace balance sheet only when provided
    if (Array.isArray(balance_sheet)) {
      await deleteByEq("balance_sheet", "company_id", companyId, "Balance Sheet");
      const rows = balance_sheet.map((b: any) => ({
        ...b,
        company_id: companyId,
        synced_at: syncedAt,
      }));
      await insertInBatches("balance_sheet", rows, "Balance Sheet");
    }

    // 8. Replace trial balance only when provided
    if (Array.isArray(trial_balance)) {
      await deleteByEq("trial_balance", "company_id", companyId, "Trial Balance");
      const rows = trial_balance.map((t: any) => ({
        ...t,
        company_id: companyId,
        synced_at: syncedAt,
      }));
      await insertInBatches("trial_balance", rows, "Trial Balance");
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
    // 1. Find company
    const { data: company, error: companyErr } = await supabase
      .from("companies")
      .select("id")
      .eq("name", company_name)
      .single();

    if (companyErr || !company) {
      return res.status(404).json({ error: "Company not found" });
    }

    // 2. Get all vouchers for this party
    const { data: vouchers, error: vErr } = await supabase
      .from("vouchers")
      .select("*, voucher_items(*)")
      .eq("company_id", company.id)
      .eq("party_name", party_name)
      .order("date", { ascending: true });

    if (vErr) {
      throw new Error(`Voucher query failed: ${vErr.message}`);
    }

    // 3. Get outstanding for this party
    const { data: outstanding, error: oErr } = await supabase
      .from("outstanding")
      .select("*")
      .eq("company_id", company.id)
      .eq("party_name", party_name);

    if (oErr) {
      throw new Error(`Outstanding query failed: ${oErr.message}`);
    }

    // 4. Get ledger info for this party
    const { data: ledger, error: lErr } = await supabase
      .from("ledgers")
      .select("*")
      .eq("company_id", company.id)
      .eq("name", party_name)
      .single();

    // 5. Build running balance
    let runningBalance = 0;
    const transactions = (vouchers || []).map((v: any) => {
      const isDebit = ["Sales", "Receipt", "Debit Note"].includes(v.voucher_type);
      const amount = Math.abs(v.amount || 0);
      runningBalance += isDebit ? amount : -amount;

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
  const { company_name } = req.query;
  const { data: company } = await supabase
    .from("companies").select("id").eq("name", company_name).single();
  if (!company) return res.status(404).json({ error: "Company not found" });
  const { data } = await supabase
    .from("vouchers").select("*, voucher_items(*)")
    .eq("company_id", company.id)
    .order("date", { ascending: false });
  res.json({ vouchers: data || [] });
});

router.get("/outstanding", requireApiKey, async (req, res) => {
  const { company_name } = req.query;
  const { data: company } = await supabase
    .from("companies").select("id").eq("name", company_name).single();
  if (!company) return res.status(404).json({ error: "Company not found" });
  const { data } = await supabase
    .from("outstanding").select("*")
    .eq("company_id", company.id)
    .order("days_overdue", { ascending: false });
  res.json({ outstanding: data || [] });
});

router.get("/stock", requireApiKey, async (req, res) => {
  const { company_name } = req.query;
  const { data: company } = await supabase
    .from("companies").select("id").eq("name", company_name).single();
  if (!company) return res.status(404).json({ error: "Company not found" });
  const { data } = await supabase
    .from("stock_items").select("*").eq("company_id", company.id);
  res.json({ stock_items: data || [] });
});

router.get("/pnl", requireApiKey, async (req, res) => {
  const { company_name } = req.query;
  const { data: company } = await supabase
    .from("companies").select("id").eq("name", company_name).single();
  if (!company) return res.status(404).json({ error: "Company not found" });
  const { data } = await supabase
    .from("profit_loss").select("*").eq("company_id", company.id);
  res.json({ profit_loss: data || [] });
});

router.get("/balance-sheet", requireApiKey, async (req, res) => {
  const { company_name } = req.query;
  const { data: company } = await supabase
    .from("companies").select("id").eq("name", company_name).single();
  if (!company) return res.status(404).json({ error: "Company not found" });
  const { data } = await supabase
    .from("balance_sheet").select("*").eq("company_id", company.id);
  res.json({ balance_sheet: data || [] });
});


router.get("/alter-ids", requireApiKey, async (req, res) => {
  const { company_name } = req.query;
  const { data } = await supabase
    .from("companies")
    .select("alter_id, alt_vch_id, alt_mst_id")
    .eq("name", company_name)
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
    const { data: company, error: companyErr } = await supabase
      .from("companies")
      .select("id")
      .eq("name", company_name)
      .single();

    if (companyErr || !company) {
      return res.status(404).json({ error: "Company not found" });
    }

    // Get all parties from ledgers (Sundry Debtors + Sundry Creditors)
    const { data: parties, error: pErr } = await supabase
      .from("ledgers")
      .select("name, group_name, opening_balance, closing_balance")
      .eq("company_id", company.id)
      .in("group_name", ["Sundry Debtors", "Sundry Creditors"]);

    if (pErr) {
      throw new Error(`Parties query failed: ${pErr.message}`);
    }

    // Get outstanding totals per party
    const { data: outstandingList, error: oErr } = await supabase
      .from("outstanding")
      .select("party_name, type, pending_amount")
      .eq("company_id", company.id);

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

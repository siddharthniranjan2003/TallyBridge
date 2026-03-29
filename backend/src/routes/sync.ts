import { Router } from "express";
import { supabase } from "../db/supabase.js";
import { requireApiKey } from "../middleware/auth.js";

const router = Router();

// ── POST /api/sync — Receive all data from desktop connector ────

router.post("/", requireApiKey, async (req, res) => {
  const {
    company_name,
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

  try {
    // 1. Upsert company
    const { data: company, error: companyErr } = await supabase
      .from("companies")
      .upsert(
        { name: company_name, last_synced_at: new Date().toISOString() },
        { onConflict: "name" }
      )
      .select("id")
      .single();

    if (companyErr || !company) {
      throw new Error(`Company upsert failed: ${companyErr?.message}`);
    }
    const company_id = company.id;

    // 2. Upsert ledgers
    if (ledgers?.length) {
      const rows = ledgers.map((l: any) => ({ ...l, company_id, synced_at: new Date().toISOString() }));
      const { error } = await supabase.from("ledgers").upsert(rows, { onConflict: "company_id,name" });
      if (error) console.error("[Sync] Ledger error:", error.message);
    }

    // 3. Upsert vouchers + items
    if (vouchers?.length) {
      for (const v of vouchers) {
        const { items, ...vData } = v;

        // Skip vouchers with no guid
        if (!vData.tally_guid) continue;

        const { data: upserted, error: vErr } = await supabase
          .from("vouchers")
          .upsert(
            { ...vData, company_id, synced_at: new Date().toISOString() },
            { onConflict: "company_id,tally_guid" }
          )
          .select("id")
          .single();

        if (vErr || !upserted) continue;

        if (items?.length) {
          // Delete old items then re-insert fresh
          await supabase.from("voucher_items").delete().eq("voucher_id", upserted.id);
          const itemRows = items
            .filter((i: any) => i.stock_item_name)
            .map((i: any) => ({ ...i, voucher_id: upserted.id }));
          if (itemRows.length) {
            await supabase.from("voucher_items").insert(itemRows);
          }
        }
      }
    }

    // 4. Upsert stock items
    if (stock_items?.length) {
      const rows = stock_items.map((s: any) => ({ ...s, company_id, synced_at: new Date().toISOString() }));
      const { error } = await supabase.from("stock_items").upsert(rows, { onConflict: "company_id,name" });
      if (error) console.error("[Sync] Stock error:", error.message);
    }

    // 5. Replace outstanding (always full refresh — delete all then insert)
    await supabase.from("outstanding").delete().eq("company_id", company_id);
    if (outstanding?.length) {
      const rows = outstanding.map((o: any) => ({ ...o, company_id, synced_at: new Date().toISOString() }));
      const { error } = await supabase.from("outstanding").insert(rows);
      if (error) console.error("[Sync] Outstanding error:", error.message);
    }

    // 6. Replace profit & loss (full refresh per sync)
    await supabase.from("profit_loss").delete().eq("company_id", company_id);
    if (profit_loss?.length) {
      const rows = profit_loss.map((p: any) => ({
        ...p,
        company_id,
        synced_at: new Date().toISOString(),
      }));
      const { error } = await supabase.from("profit_loss").insert(rows);
      if (error) console.error("[Sync] P&L error:", error.message);
    }

    // 7. Replace balance sheet (full refresh per sync)
    await supabase.from("balance_sheet").delete().eq("company_id", company_id);
    if (balance_sheet?.length) {
      const rows = balance_sheet.map((b: any) => ({
        ...b,
        company_id,
        synced_at: new Date().toISOString(),
      }));
      const { error } = await supabase.from("balance_sheet").insert(rows);
      if (error) console.error("[Sync] Balance Sheet error:", error.message);
    }

    // 8. Replace trial balance (full refresh per sync)
    await supabase.from("trial_balance").delete().eq("company_id", company_id);
    if (trial_balance?.length) {
      const rows = trial_balance.map((t: any) => ({
        ...t,
        company_id,
        synced_at: new Date().toISOString(),
      }));
      const { error } = await supabase.from("trial_balance").insert(rows);
      if (error) console.error("[Sync] Trial Balance error:", error.message);
    }

    // 9. Log the sync
    await supabase.from("sync_log").insert({
      company_id,
      status: "success",
      records_synced: {
        ledgers: ledgers?.length || 0,
        vouchers: vouchers?.length || 0,
        stock_items: stock_items?.length || 0,
        outstanding: outstanding?.length || 0,
        profit_loss: profit_loss?.length || 0,
        balance_sheet: balance_sheet?.length || 0,
        trial_balance: trial_balance?.length || 0,
      },
    });

    res.json({
      success: true,
      company_id,
      records: {
        ledgers: ledgers?.length || 0,
        vouchers: vouchers?.length || 0,
        stock_items: stock_items?.length || 0,
        outstanding: outstanding?.length || 0,
        profit_loss: profit_loss?.length || 0,
        balance_sheet: balance_sheet?.length || 0,
        trial_balance: trial_balance?.length || 0,
      },
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
import { Router } from "express";
import { supabase } from "../db/supabase.js";
import { requireApiKey } from "../middleware/auth.js";

const router = Router();

router.post("/", requireApiKey, async (req, res) => {
  const { company_name, ledgers, vouchers, stock_items, outstanding } = req.body;

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

    // 6. Log the sync
    await supabase.from("sync_log").insert({
      company_id,
      status: "success",
      records_synced: {
        ledgers: ledgers?.length || 0,
        vouchers: vouchers?.length || 0,
        stock_items: stock_items?.length || 0,
        outstanding: outstanding?.length || 0,
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
      },
    });
  } catch (err: any) {
    console.error("[Sync] Error:", err.message);
    res.status(500).json({ error: err.message });
  }
});

export default router;
#!/usr/bin/env node
/**
 * Exercises the desktop app's DIRECT/HYBRID ingest path against the local stack,
 * without needing TallyPrime to be running.
 *
 * Why this is worth its own script: pointing the backend at local proves the READ
 * side (reports, stock). It proves nothing about the WRITE side, because in
 * `hybrid` and `direct` ingest modes the Python engine does not go through the
 * backend at all — it calls PostgREST itself with SYNC_INGEST_URL /
 * SYNC_INGEST_KEY (cloud_pusher.py:229). syncIngestMode defaults to "hybrid"
 * (store.ts:78), so that is the live path on a default install. Configure the
 * backend and forget this, and the app happily keeps writing masters to the cloud
 * while every report reads local.
 *
 * It replicates exactly what cloud_pusher.py does:
 *   1. the same headers   -- apikey + Authorization: Bearer  (_postgrest_headers)
 *   2. resolve the company by guid, then by name             (_resolve_direct_company_id)
 *   3. call the tb_ingest_masters RPC                        (_post_direct_via_postgrest)
 *
 * It writes one stock item under a SENTINEL group and deletes it again, so it is
 * safe to run against a populated database. Nothing else is touched.
 */

import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { resolveStackEnv, gatewayUrl } from "./env.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, "..", "..");

// Works against either stack. TB_ENV_FILE names one explicitly; otherwise the
// Docker stack, then the native one, then a packaged stack beside this script.
const { values: env } = resolveStackEnv(
  join(REPO, "local-supabase", ".env"),
  join(REPO, "native-stack", "config", ".env"),
  join(HERE, "..", ".env"),
  join(HERE, "..", "config", ".env"),
);
const KEY = env.SERVICE_ROLE_KEY;

// Deliberately built the same wrong-looking way the engine builds it: take the
// configured syncIngestUrl and strip at "/functions/". If this stops matching
// cloud_pusher.py:224 the whole check is worthless, so it is spelled out here
// rather than hardcoded to the REST URL.
const SYNC_INGEST_URL = `${gatewayUrl(env)}/functions/v1/ingest-sync`;
const REST = SYNC_INGEST_URL.includes("/functions/")
  ? SYNC_INGEST_URL.split("/functions/")[0] + "/rest/v1"
  : "";

if (!REST) {
  console.error("SYNC_INGEST_URL has no /functions/ segment; the engine would refuse this too.");
  process.exit(1);
}

const headers = (extra = {}) => ({
  apikey: KEY,
  Authorization: `Bearer ${KEY}`,
  "Content-Type": "application/json",
  ...extra,
});

const COMPANY_GUID = process.argv[2] || "f3e9df46-fc4f-4b85-930a-abe1e427e900";
const SENTINEL = `ZZZ INGEST-PATH CHECK ${Date.now()}`;
let failed = false;

function step(label, ok, detail = "") {
  console.log(`  ${ok ? "ok  " : "FAIL"} ${label}${detail ? "   " + detail : ""}`);
  if (!ok) failed = true;
}

console.log(`SYNC_INGEST_URL  ${SYNC_INGEST_URL}`);
console.log(`derived REST     ${REST}\n`);

// 1. company resolution by guid, exactly as _resolve_direct_company_id does
const compRes = await fetch(`${REST}/companies?guid=eq.${COMPANY_GUID}&select=id,name`, {
  headers: headers(),
});
const companies = compRes.ok ? await compRes.json() : [];
step("resolve company by guid", compRes.ok && companies.length === 1,
  compRes.ok ? (companies[0] ? `${companies[0].name} -> ${companies[0].id}` : "no match") : `http ${compRes.status}`);

if (!companies.length) {
  console.log("\ncannot continue without a company; run copy-live-to-local.mjs first.");
  process.exit(1);
}
const companyId = companies[0].id;

// 2. the masters RPC — the call that actually carries a sync's groups/ledgers/stock
const rpcRes = await fetch(`${REST}/rpc/tb_ingest_masters`, {
  method: "POST",
  headers: headers(),
  body: JSON.stringify({
    p_company_id: companyId,
    p_stock_items: [{
      name: SENTINEL,
      group_name: "SENTINEL",
      unit: "NOS",
      closing_qty: "1",
      closing_value: "0",
      rate: "0",
      part_code: "SENTINEL-PART",
    }],
  }),
});
const rpcBody = rpcRes.ok ? await rpcRes.json() : await rpcRes.text();
step("POST /rpc/tb_ingest_masters", rpcRes.ok, rpcRes.ok ? JSON.stringify(rpcBody) : `http ${rpcRes.status} ${rpcBody}`);

// 3. read it back, including part_code — the column added by
//    20260619_stock_items_part_code.sql, which is also the migration that proves
//    the redeployed RPC (not the 20260427 original) is the one in place.
const backRes = await fetch(
  `${REST}/stock_items?name=eq.${encodeURIComponent(SENTINEL)}&select=name,unit,closing_qty,part_code`,
  { headers: headers() },
);
const back = backRes.ok ? await backRes.json() : [];
step("row written by the RPC is readable", back.length === 1,
  back.length ? JSON.stringify(back[0]) : `http ${backRes.status}`);
step("part_code populated (proves the 20260619 RPC redeploy applied)",
  back.length === 1 && back[0].part_code === "SENTINEL-PART");

// 4. clean up
const delRes = await fetch(`${REST}/stock_items?group_name=eq.SENTINEL`, {
  method: "DELETE",
  headers: headers({ Prefer: "return=minimal" }),
});
step("sentinel removed", delRes.ok, `http ${delRes.status}`);

console.log(failed
  ? "\nINGEST PATH: BROKEN — the desktop app would fail to write to local."
  : "\nINGEST PATH OK — hybrid/direct ingest writes land in the local stack.");
// exitCode rather than process.exit(): fetch() leaves keep-alive sockets in
// undici's pool, and tearing the loop down under them crashes Node 24 on Windows
// with an assertion and a bogus exit code.
process.exitCode = failed ? 1 : 0;

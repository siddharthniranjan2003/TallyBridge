#!/usr/bin/env node
/**
 * End-to-end health check for the local stack. Run it after install, and run it
 * first whenever something looks wrong.
 *
 * Each check is written so that FAILING IS LOUD. The failure mode this whole
 * setup is prone to is the quiet one: a component that is misconfigured but still
 * answers 200, because it fell back to a default. So the checks below assert on
 * what came back, not merely that something came back:
 *
 *   - Kong must REFUSE an unauthenticated request. A 200 there means key-auth is
 *     not applied and the ledger is readable by anything that can reach the port.
 *   - pg-meta must refuse the anon key. A 200 there is an unauthenticated admin
 *     API over the whole database.
 *   - The reports must return non-zero rows. An empty 200 means the schema is
 *     there but the data never landed.
 */

import { readFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const PKG = join(HERE, "..");

function readEnvFile(path) {
  if (!existsSync(path)) return {};
  const out = {};
  for (const line of readFileSync(path, "utf8").split(/\r?\n/)) {
    const t = line.trim();
    if (!t || t.startsWith("#")) continue;
    const eq = t.indexOf("=");
    if (eq === -1) continue;
    out[t.slice(0, eq).trim()] = t.slice(eq + 1).trim();
  }
  return out;
}

const env = readEnvFile(join(PKG, ".env"));
const KONG = `http://127.0.0.1:${env.KONG_HTTP_PORT || 54321}`;
const STUDIO = `http://127.0.0.1:${env.STUDIO_PORT || 54323}`;
const BACKEND = process.env.TB_BACKEND_URL || "http://localhost:3001";
const BACKEND_KEY = process.env.TB_BACKEND_KEY || "localdevkey";
const SERVICE = env.SERVICE_ROLE_KEY;
const ANON = env.ANON_KEY;

let failures = 0;
let warnings = 0;

function pass(label, detail = "") { console.log(`  ok    ${label}${detail ? "   " + detail : ""}`); }
function fail(label, detail = "") { console.log(`  FAIL  ${label}${detail ? "   " + detail : ""}`); failures += 1; }
function warn(label, detail = "") { console.log(`  warn  ${label}${detail ? "   " + detail : ""}`); warnings += 1; }

async function tryFetch(url, opts = {}) {
  try {
    return { res: await fetch(url, { signal: AbortSignal.timeout(20000), ...opts }) };
  } catch (err) {
    return { err };
  }
}

const svc = { apikey: SERVICE, Authorization: `Bearer ${SERVICE}` };

/**
 * A backend holding the wrong Supabase key does NOT report an auth error.
 * Kong answers 401 "Invalid authentication credentials", supabase-js turns that
 * into an empty result, and the route reports `404 {"error":"Company not found"}`
 * — which reads like a missing company rather than a key problem. This is the
 * exact failure produced by re-running bootstrap --rotate, or by reinstalling the
 * stack, without restarting the backend afterwards.
 */
const STALE_KEY_HINT =
  '  <- 404 here is usually a STALE KEY, not a missing company. Kong 401s the old key and the route reports "Company not found". Restart the backend: cd ..\\backend ; . .\\use-local.ps1 ; npm run dev';

console.log("\nSUPABASE");

// 1. REST reachable with the service key
{
  const { res, err } = await tryFetch(`${KONG}/rest/v1/companies?select=id,name`, { headers: svc });
  if (err) fail("kong -> postgrest", err.message);
  else if (!res.ok) fail("kong -> postgrest", `http ${res.status}`);
  else {
    const rows = await res.json();
    if (!rows.length) fail("kong -> postgrest", "reachable but no companies row — data was never loaded");
    else pass("kong -> postgrest", `${rows.length} company: ${rows.map((r) => r.name).join(", ")}`);
  }
}

// 2. Kong must reject a request with no key at all.
{
  const { res, err } = await tryFetch(`${KONG}/rest/v1/companies?select=id`);
  if (err) fail("kong rejects unauthenticated", err.message);
  else if (res.status === 401) pass("kong rejects unauthenticated", "401");
  else fail("kong rejects unauthenticated", `got ${res.status} — key-auth is NOT protecting /rest/v1`);
}

// 3. pg-meta must be admin-only.
{
  const { res } = await tryFetch(`${KONG}/pg/tables?limit=1`, { headers: { apikey: ANON } });
  if (res && res.status === 403) pass("pg-meta refuses the anon key", "403");
  else fail("pg-meta refuses the anon key", `got ${res ? res.status : "network error"} — anon can administer the DB`);
}

// 4. Studio
{
  const { res, err } = await tryFetch(`${STUDIO}/`, { redirect: "follow" });
  if (err) fail("studio", err.message);
  else if (res.ok) pass("studio", `${STUDIO}  (http ${res.status})`);
  else fail("studio", `http ${res.status}`);
}

// 5. Row counts for the tables the reports actually read.
console.log("\nDATA");
for (const table of ["companies", "stock_items", "vouchers", "voucher_items", "ledgers", "outstanding"]) {
  const { res, err } = await tryFetch(`${KONG}/rest/v1/${table}?select=*`, {
    headers: { ...svc, Range: "0-0", Prefer: "count=exact" },
  });
  if (err || !res.ok) { fail(table.padEnd(22), err ? err.message : `http ${res.status}`); continue; }
  const total = Number((res.headers.get("content-range") || "").split("/")[1]);
  if (!Number.isFinite(total) || total === 0) fail(table.padEnd(22), "0 rows");
  else pass(table.padEnd(22), `${total.toLocaleString()} rows`);
}

// 6. The two endpoints the client actually uses. Backend down is a warning, not a
//    failure: the Supabase stack can be perfectly healthy on its own, and this
//    script is also used to check the stack before the backend is started.
console.log("\nBACKEND (reports)");
const bh = { "x-api-key": BACKEND_KEY };
const { res: cRes, err: cErr } = await tryFetch(`${KONG}/rest/v1/companies?select=id&limit=1`, { headers: svc });
const companyId = cRes && cRes.ok ? (await cRes.json())[0]?.id : null;

if (!companyId) {
  warn("backend checks skipped", "no company id available");
} else {
  {
    const { res, err } = await tryFetch(`${BACKEND}/api/sync/stock?company_id=${companyId}`, { headers: bh });
    if (err) warn("GET /api/sync/stock", `${err.message} — is the backend running?`);
    else if (!res.ok) fail("GET /api/sync/stock", `http ${res.status}${res.status === 404 ? STALE_KEY_HINT : ""}`);
    else {
      const body = await res.json();
      const n = (body.stock_items || []).length;
      n ? pass("GET /api/sync/stock", `${n.toLocaleString()} stock items`)
        : fail("GET /api/sync/stock", "200 but zero items");
    }
  }
  {
    const started = Date.now();
    const { res, err } = await tryFetch(`${BACKEND}/api/sync/reorder-levels?company_id=${companyId}`, { headers: bh });
    if (err) warn("GET /api/sync/reorder-levels", `${err.message} — is the backend running?`);
    else if (!res.ok) fail("GET /api/sync/reorder-levels", `http ${res.status}${res.status === 404 ? STALE_KEY_HINT : ""}`);
    else {
      const body = await res.json();
      const scanned = body.total_items_scanned || 0;
      const priced = (body.items || []).filter((i) => Number(i.purchase_rate) > 0).length;
      // A report that scans rows but prices none means voucher_items or vouchers
      // did not come across — the report still returns 200 and looks fine.
      if (!scanned) fail("GET /api/sync/reorder-levels", "200 but scanned 0 items");
      else if (!priced) fail("GET /api/sync/reorder-levels", `scanned ${scanned} but priced 0 — voucher data missing`);
      else pass("GET /api/sync/reorder-levels",
        `${scanned.toLocaleString()} scanned, ${priced.toLocaleString()} priced, ${body.needs_reorder_count} need reorder, ${((Date.now() - started) / 1000).toFixed(1)}s`);
    }
  }
}

console.log(
  failures
    ? `\n${failures} FAILURE(S)${warnings ? `, ${warnings} warning(s)` : ""}\n`
    : `\nAll checks passed${warnings ? ` (${warnings} warning(s))` : ""}.\n`,
);
process.exit(failures ? 1 : 0);

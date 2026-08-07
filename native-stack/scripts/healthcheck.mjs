#!/usr/bin/env node
/**
 * End-to-end health check for the native stack.
 *
 * Same philosophy as the Docker stack's: every check asserts on what came back,
 * because the failure mode here is a component that is misconfigured and still
 * answers 200.
 *
 * Two checks exist here that have no Docker equivalent, and they are the most
 * important ones in the file:
 *
 *   - THE ANON CHECK. The Docker stack has Kong in front of PostgREST rejecting
 *     unauthenticated requests. This stack does not — Caddy rewrites and proxies,
 *     it does not authenticate. The only thing standing between an open port and
 *     the client's whole ledger is that `anon` holds no table privileges
 *     (903_lockdown_anon.sql). Measured before that file existed, an
 *     unauthenticated GET returned the stock table. So this is verified on every
 *     run, not assumed.
 *
 *   - THE REWRITE CHECK. PostgREST has no url-prefix option, so if Caddy is down
 *     or its Caddyfile used `handle` instead of `handle_path`, every query 404s.
 *     That was the defect in design v1.
 */

import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { readEnvFile } from "../../local-stack-shared/scripts/env.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const PKG = join(HERE, "..");

const envPath = join(PKG, "config", ".env");
if (!existsSync(envPath)) {
  console.error("config/.env not found — run: node scripts/bootstrap.mjs");
  process.exit(2);
}
const env = readEnvFile(envPath);

const GATEWAY = `http://127.0.0.1:${env.GATEWAY_PORT}`;
const POSTGREST = `http://127.0.0.1:${env.POSTGREST_PORT}`;
const BACKEND = process.env.TB_BACKEND_URL || `http://localhost:${env.BACKEND_PORT || 3001}`;
const BACKEND_KEY = process.env.TB_BACKEND_KEY || "localdevkey";
const SERVICE = env.SERVICE_ROLE_KEY;
const ANON = env.ANON_KEY;

/**
 * An install that was given no project to copy from is SUPPOSED to have empty
 * tables. Without this the installer finishes a perfectly good empty install and
 * then reports seven failures, which reads as a broken install.
 *
 * It only downgrades the row-count and company-lookup checks. The auth-boundary
 * and gateway checks stay hard failures either way — those are wrong regardless
 * of whether any data has landed.
 */
const allowEmpty = process.argv.includes("--allow-empty");

let failures = 0;
let warnings = 0;
const pass = (l, d = "") => console.log(`  ok    ${l}${d ? "   " + d : ""}`);
const fail = (l, d = "") => { console.log(`  FAIL  ${l}${d ? "   " + d : ""}`); failures += 1; };
const warn = (l, d = "") => { console.log(`  warn  ${l}${d ? "   " + d : ""}`); warnings += 1; };

async function tryFetch(url, opts = {}) {
  try {
    return { res: await fetch(url, { signal: AbortSignal.timeout(20000), ...opts }) };
  } catch (err) {
    return { err };
  }
}

const svc = { apikey: SERVICE, Authorization: `Bearer ${SERVICE}` };

const STALE_KEY_HINT =
  '  <- 404 here is usually a STALE KEY, not a missing company. PostgREST 401s the old key and the route reports "Company not found". Restart the backend after any bootstrap --rotate.';

console.log("\nSERVICES");
{
  const { res, err } = await tryFetch(`${POSTGREST}/companies?select=id,name`, { headers: svc });
  if (err) fail("postgrest", `${err.message} — is it running? .\\start.ps1`);
  else if (!res.ok) fail("postgrest", `http ${res.status}`);
  else {
    const rows = await res.json();
    if (rows.length) pass("postgrest", `${rows.length} company: ${rows.map((r) => r.name).join(", ")}`);
    else if (allowEmpty) pass("postgrest", "reachable (database is empty, as expected)");
    else fail("postgrest", "reachable but no companies row — data was never loaded");
  }
}
{
  const { res, err } = await tryFetch(`${GATEWAY}/rest/v1/companies?select=id`, { headers: svc });
  if (err) fail("caddy strips /rest/v1", `${err.message} — is the gateway running?`);
  else if (res.ok) pass("caddy strips /rest/v1", `${GATEWAY}/rest/v1 -> :${env.POSTGREST_PORT}`);
  else fail("caddy strips /rest/v1", `http ${res.status} — check handle_path vs handle in the Caddyfile`);
}

console.log("\nAUTH BOUNDARY   (no Kong here — anon privileges are the only gate)");
{
  const { res } = await tryFetch(`${GATEWAY}/rest/v1/stock_items?select=name&limit=1`);
  if (!res) fail("unauthenticated request is refused", "network error");
  else if (res.status === 200) {
    fail("unauthenticated request is refused",
      "200 — THE LEDGER IS READABLE WITH NO KEY. Re-apply 903_lockdown_anon.sql.");
  } else pass("unauthenticated request is refused", `http ${res.status}`);
}
{
  const { res } = await tryFetch(`${GATEWAY}/rest/v1/stock_items?select=name&limit=1`, {
    headers: { apikey: ANON, Authorization: `Bearer ${ANON}` },
  });
  if (!res) fail("anon key cannot read tables", "network error");
  else if (res.status === 200) {
    fail("anon key cannot read tables", "200 — anon still holds table grants.");
  } else pass("anon key cannot read tables", `http ${res.status}`);
}

console.log(`\nDATA${allowEmpty ? "   (empty is expected: no project was given to copy from)" : ""}`);
for (const table of ["companies", "stock_items", "vouchers", "voucher_items", "ledgers", "outstanding"]) {
  const { res, err } = await tryFetch(`${GATEWAY}/rest/v1/${table}?select=*`, {
    headers: { ...svc, Range: "0-0", Prefer: "count=exact" },
  });
  // A failed REQUEST is always a failure — that means the table is missing or
  // unreachable, which is a schema problem, not a data one.
  if (err || !res.ok) { fail(table.padEnd(22), err ? err.message : `http ${res.status}`); continue; }
  const total = Number((res.headers.get("content-range") || "").split("/")[1]);
  if (Number.isFinite(total) && total > 0) pass(table.padEnd(22), `${total.toLocaleString()} rows`);
  else if (allowEmpty) pass(table.padEnd(22), "0 rows (table exists)");
  else fail(table.padEnd(22), "0 rows");
}

console.log("\nBACKEND (reports)");
const { res: cRes } = await tryFetch(`${GATEWAY}/rest/v1/companies?select=id&limit=1`, { headers: svc });
const companyId = cRes && cRes.ok ? (await cRes.json())[0]?.id : null;

if (!companyId) {
  warn("backend checks skipped", "no company id available");
} else {
  {
    const { res, err } = await tryFetch(`${BACKEND}/api/sync/stock?company_id=${companyId}`, {
      headers: { "x-api-key": BACKEND_KEY },
    });
    if (err) warn("GET /api/sync/stock", `${err.message} — is the backend running?`);
    else if (!res.ok) fail("GET /api/sync/stock", `http ${res.status}${res.status === 404 ? STALE_KEY_HINT : ""}`);
    else {
      const n = ((await res.json()).stock_items || []).length;
      n ? pass("GET /api/sync/stock", `${n.toLocaleString()} stock items`)
        : fail("GET /api/sync/stock", "200 but zero items");
    }
  }
  {
    const started = Date.now();
    const { res, err } = await tryFetch(`${BACKEND}/api/sync/reorder-levels?company_id=${companyId}`, {
      headers: { "x-api-key": BACKEND_KEY },
    });
    if (err) warn("GET /api/sync/reorder-levels", `${err.message} — is the backend running?`);
    else if (!res.ok) fail("GET /api/sync/reorder-levels", `http ${res.status}${res.status === 404 ? STALE_KEY_HINT : ""}`);
    else {
      const body = await res.json();
      const scanned = body.total_items_scanned || 0;
      const priced = (body.items || []).filter((i) => Number(i.purchase_rate) > 0).length;
      // Scanning rows but pricing none means vouchers/voucher_items never landed;
      // the report still returns 200 and looks healthy.
      if (!scanned) fail("GET /api/sync/reorder-levels", "200 but scanned 0 items");
      else if (!priced) fail("GET /api/sync/reorder-levels", `scanned ${scanned} but priced 0 — voucher data missing`);
      else pass("GET /api/sync/reorder-levels",
        `${scanned.toLocaleString()} scanned, ${priced.toLocaleString()} priced, ${body.needs_reorder_count} need reorder, ${((Date.now() - started) / 1000).toFixed(1)}s`);
    }
  }
}

console.log("\nSTARTUP MODE");
{
  // The whole point of this stack. In process mode the reports die the moment
  // the operator logs out, which is the problem Docker already had.
  const { res } = await tryFetch(`${POSTGREST}/`, { headers: svc });
  void res;
  const svcMode = existsSync(join(PKG, "logs", "postgrest.pid")) ? "process" : "service";
  svcMode === "service"
    ? pass("running as Windows services", "survives a login-less reboot")
    : warn("running as processes, not services",
        "tied to this login session — run install.ps1 elevated to register the services");
}

console.log(
  failures
    ? `\n${failures} FAILURE(S)${warnings ? `, ${warnings} warning(s)` : ""}\n`
    : `\nAll checks passed${warnings ? ` (${warnings} warning(s))` : ""}.\n`,
);
process.exitCode = failures ? 1 : 0;

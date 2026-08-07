#!/usr/bin/env node
/**
 * Copies data from the live Supabase project into the local stack.
 *
 * This is both the way to get a realistic dev database and the intended cutover
 * mechanism for moving a client off the cloud: run it once against the client's
 * project, point the backend at local, done.
 *
 * Both ends are spoken to over PostgREST, so no Postgres password is needed on
 * either side — only the service_role keys, which is all that is available for
 * the live project (see schema-parity.mjs for why).
 *
 * Primary keys are carried across verbatim. That is what makes foreign keys
 * resolve (voucher_items.voucher_id must still point at the same vouchers.id) and
 * what makes re-running safe: writes use `Prefer: resolution=merge-duplicates`,
 * so a second run updates rows in place instead of duplicating them. Nothing is
 * ever deleted locally.
 *
 * Usage:
 *   node scripts/copy-live-to-local.mjs
 *   node scripts/copy-live-to-local.mjs --only companies,stock_items
 *   node scripts/copy-live-to-local.mjs --profile reports
 */

import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { readEnvFile, resolveStackEnv, gatewayUrl } from "./env.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, "..", "..");

// Which stack are we filling? TB_ENV_FILE names it explicitly; otherwise fall
// back to the Docker stack, then to a packaged stack sitting beside this script.
const { path: envPath, values: localEnv } = resolveStackEnv(
  join(REPO, "local-supabase", ".env"),
  join(REPO, "native-stack", "config", ".env"),
  join(HERE, "..", ".env"),
  join(HERE, "..", "config", ".env"),
);
const liveEnv = readEnvFile(join(REPO, "backend", ".env"));

const LOCAL_URL = process.env.LOCAL_SUPABASE_URL || gatewayUrl(localEnv);
const LOCAL_KEY = localEnv.SERVICE_ROLE_KEY;
const LIVE_URL = process.env.LIVE_SUPABASE_URL || liveEnv.SUPABASE_URL;
const LIVE_KEY = process.env.LIVE_SUPABASE_KEY || liveEnv.SUPABASE_SERVICE_KEY;

/**
 * ORDER IS A FOREIGN-KEY ORDER, NOT ALPHABETICAL. companies must land before
 * anything that references it, and vouchers before voucher_items /
 * voucher_ledger_entries / purchases, or every insert fails on a FK violation.
 *
 * `purchase_matching_api` is deliberately absent: it is a view over
 * Purchase_Matching, so copying it would either fail or double-write the base
 * table.
 */
const TABLES = [
  "companies",
  "groups",
  "ledgers",
  "stock_items",
  "outstanding",
  "profit_loss",
  "balance_sheet",
  "trial_balance",
  "vouchers",
  "voucher_items",
  "voucher_ledger_entries",
  "purchases",
  "sync_log",
  "push_queue",
  "scan_jobs",
  "Purchase_Matching",
  "Audit_Trail_Purchase",
];

/**
 * Tables whose primary key is `GENERATED ALWAYS AS IDENTITY`, which PostgREST
 * cannot write to at all:
 *
 *   {"code":"428C9","message":"cannot insert a non-DEFAULT value into column \"id\"",
 *    "hint":"Use OVERRIDING SYSTEM VALUE to override."}
 *
 * PostgREST has no way to emit OVERRIDING SYSTEM VALUE, so `id` must be dropped
 * from the payload and left to the local sequence. Two consequences:
 *
 *   - Local ids will not match live ids. Safe here because nothing references
 *     Audit_Trail_Purchase by id — it is an append-only OCR audit trail, read by
 *     paging the whole table (backend/supabase_audit_trail_purchase.sql:33).
 *   - Without the PK, merge-duplicates has nothing to merge on, so a re-run would
 *     append the whole table a second time. These tables are therefore replaced
 *     wholesale rather than upserted.
 *
 * Rows are read ordered by id so relative order survives the renumbering — the
 * parser resolves conflicts by "newest row wins" (highest id), so scrambling the
 * order would silently change which curated mapping applies.
 */
const IDENTITY_PK_TABLES = new Set(["Audit_Trail_Purchase"]);

// Everything the MRP/reorder report and the stock/dashboard endpoints read.
// Drops the OCR lookup tables and the outbound push queue, which belong to the
// Sale/Purchase features that are switched off for this client.
const PROFILES = {
  reports: [
    "companies", "groups", "ledgers", "stock_items", "outstanding",
    "profit_loss", "balance_sheet", "trial_balance",
    "vouchers", "voucher_items", "voucher_ledger_entries", "purchases",
  ],
};

const args = process.argv.slice(2);
const flag = (name) => {
  const i = args.indexOf(name);
  return i === -1 ? null : args[i + 1];
};

let tables = TABLES;
const profile = flag("--profile");
if (profile) {
  if (!PROFILES[profile]) {
    console.error(`unknown profile "${profile}" — known: ${Object.keys(PROFILES).join(", ")}`);
    process.exit(2);
  }
  tables = PROFILES[profile];
}
const only = flag("--only");
if (only) {
  const wanted = new Set(only.split(",").map((s) => s.trim()));
  // Filter TABLES rather than using the given order, so FK order is preserved
  // however the caller listed them.
  tables = TABLES.filter((t) => wanted.has(t));
}
const skip = flag("--skip");
if (skip) {
  const unwanted = new Set(skip.split(",").map((s) => s.trim()));
  tables = tables.filter((t) => !unwanted.has(t));
}

if (!LOCAL_KEY) {
  console.error(`no SERVICE_ROLE_KEY found (looked at: ${envPath || "nothing"}) — run the stack's bootstrap.mjs`);
  process.exit(2);
}
if (!LIVE_URL || !LIVE_KEY) {
  console.error("no SUPABASE_URL / SUPABASE_SERVICE_KEY in backend/.env");
  process.exit(2);
}

const READ_PAGE = 1000;  // PostgREST's own max-rows on the hosted project
const WRITE_BATCH = 500; // keeps request bodies well under Kong's buffer limits

const headers = (key, extra = {}) => ({
  apikey: key,
  Authorization: `Bearer ${key}`,
  "Content-Type": "application/json",
  ...extra,
});

async function readPage(table, from, to) {
  // order=id.asc is not cosmetic. Range pagination over an unordered query gives
  // Postgres licence to return rows in a different order per page, which silently
  // drops some rows and repeats others. Every table here has an `id`.
  const url = `${LIVE_URL}/rest/v1/${encodeURIComponent(table)}?select=*&order=id.asc`;
  const res = await fetch(url, {
    headers: headers(LIVE_KEY, { Range: `${from}-${to}`, "Range-Unit": "items" }),
  });
  if (!res.ok) throw new Error(`read ${table} [${from}-${to}]: ${res.status} ${await res.text()}`);
  return res.json();
}

async function writeBatch(table, rows) {
  const url = `${LOCAL_URL}/rest/v1/${encodeURIComponent(table)}`;
  const payload = IDENTITY_PK_TABLES.has(table)
    ? rows.map(({ id, ...rest }) => rest) // see IDENTITY_PK_TABLES
    : rows;
  const res = await fetch(url, {
    method: "POST",
    headers: headers(LOCAL_KEY, {
      // merge-duplicates = upsert on the primary key, which is why re-running is
      // safe. return=minimal keeps PostgREST from echoing every row back.
      Prefer: "resolution=merge-duplicates,return=minimal",
    }),
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`write ${table}: ${res.status} ${await res.text()}`);
}

// Only for IDENTITY_PK_TABLES, where upsert is impossible and a re-run would
// otherwise append a duplicate copy of the whole table.
async function clearTable(table) {
  const res = await fetch(`${LOCAL_URL}/rest/v1/${encodeURIComponent(table)}?id=gt.0`, {
    method: "DELETE",
    headers: headers(LOCAL_KEY, { Prefer: "return=minimal" }),
  });
  if (!res.ok) throw new Error(`clear ${table}: ${res.status} ${await res.text()}`);
}

async function localCount(table) {
  const res = await fetch(`${LOCAL_URL}/rest/v1/${encodeURIComponent(table)}?select=*`, {
    headers: headers(LOCAL_KEY, { Range: "0-0", Prefer: "count=exact" }),
  });
  const range = res.headers.get("content-range") || "";
  return range.split("/")[1] || "?";
}

console.log(`live   ${LIVE_URL}`);
console.log(`local  ${LOCAL_URL}`);
console.log(`tables ${tables.length}${profile ? ` (profile: ${profile})` : ""}\n`);

const started = Date.now();
const summary = [];

for (const table of tables) {
  process.stdout.write(`${table.padEnd(24)}`);
  let from = 0;
  let copied = 0;
  try {
    if (IDENTITY_PK_TABLES.has(table)) await clearTable(table);
    for (;;) {
      const page = await readPage(table, from, from + READ_PAGE - 1);
      if (!page.length) break;
      for (let i = 0; i < page.length; i += WRITE_BATCH) {
        await writeBatch(table, page.slice(i, i + WRITE_BATCH));
      }
      copied += page.length;
      from += page.length;
      process.stdout.write(`\r${table.padEnd(24)}${copied} rows...`);
      if (page.length < READ_PAGE) break;
    }
    const local = await localCount(table);
    const match = String(local) === String(copied) ? "" : `  (local total ${local})`;
    console.log(`\r${table.padEnd(24)}${String(copied).padStart(7)} rows${match}          `);
    summary.push([table, copied, local]);
  } catch (err) {
    console.log(`\r${table.padEnd(24)}FAILED`);
    console.error(`    ${err.message}`);
    process.exitCode = 1;
  }
}

const total = summary.reduce((n, [, c]) => n + c, 0);
console.log(`\n${total.toLocaleString()} rows in ${((Date.now() - started) / 1000).toFixed(1)}s`);

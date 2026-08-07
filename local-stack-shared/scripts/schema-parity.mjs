#!/usr/bin/env node
/**
 * Diffs the local database against the live testing project and exits non-zero
 * on any difference.
 *
 * ── Why it reads OpenAPI instead of pg_dump ──────────────────────────────────
 *
 * The obvious check is `pg_dump --schema-only` on both sides. That needs the live
 * Postgres password, which is not available: the Dashboard account that owns
 * yynuuysvjeipawzfbeme is niranjansiddharth0@gmail.com and is not accessible, and
 * the logged-in CLI account cannot see the project either. What IS available is
 * the service_role key in backend/.env, and PostgREST publishes a complete
 * OpenAPI description of every table, column and RPC it exposes. That is enough
 * to catch the drift that actually happened here — objects created by hand in the
 * SQL Editor and never written to a .sql file.
 *
 * ── State the limit whenever you report parity ───────────────────────────────
 *
 * This compares tables, columns, column types and RPC names/arguments. It CANNOT
 * see indexes, constraints, triggers, defaults, RLS policies, or anything outside
 * the `public` schema. "PARITY" from this script means the REST surface matches,
 * not that the two databases are identical. If the live DB password is ever
 * recovered, a real pg_dump diff remains the stronger check.
 */

import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { readEnvFile, resolveStackEnv, gatewayUrl } from "./env.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
// Works from local-stack-shared/scripts/ in the repo and from scripts/ inside
// either packaged stack, so the repo root is two or three levels up depending on
// layout; both candidate .env paths below are tried and the first that exists wins.
const REPO = join(HERE, "..", "..");

// Which stack are we checking? TB_ENV_FILE names it explicitly; otherwise fall
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

if (!LOCAL_KEY) {
  console.error(`no SERVICE_ROLE_KEY found (looked at: ${envPath || "nothing"}) — run the stack's bootstrap.mjs`);
  process.exit(2);
}
if (!LIVE_URL || !LIVE_KEY) {
  console.error("no SUPABASE_URL / SUPABASE_SERVICE_KEY in backend/.env — cannot reach live to compare");
  process.exit(2);
}

async function spec(url, key, label) {
  const res = await fetch(`${url}/rest/v1/`, {
    headers: { apikey: key, Authorization: `Bearer ${key}`, Accept: "application/openapi+json" },
  });
  if (!res.ok) throw new Error(`${label}: ${res.status} ${res.statusText} from ${url}/rest/v1/`);
  return res.json();
}

// Columns come back as {format, ...}; compare the Postgres type name only.
// PostgREST reports "timestamp with time zone" identically on both sides, so a
// plain string compare is enough and does not need a type-alias table.
function shape(s) {
  const tables = new Map();
  for (const [name, def] of Object.entries(s.definitions || {})) {
    const cols = new Map();
    for (const [col, p] of Object.entries(def.properties || {})) cols.set(col, p.format || "?");
    tables.set(name, cols);
  }
  const rpcs = new Map();
  for (const [path, def] of Object.entries(s.paths || {})) {
    if (!path.startsWith("/rpc/")) continue;
    const props = def.post?.parameters?.find((p) => p.in === "body")?.schema?.properties || {};
    rpcs.set(path.slice(5), Object.keys(props).sort().join(","));
  }
  return { tables, rpcs };
}

const [liveSpec, localSpec] = await Promise.all([
  spec(LIVE_URL, LIVE_KEY, "live"),
  spec(LOCAL_URL, LOCAL_KEY, "local"),
]);
const live = shape(liveSpec);
const local = shape(localSpec);

const problems = [];
const only = (a, b) => [...a.keys()].filter((k) => !b.has(k)).sort();

console.log(`live   ${LIVE_URL}`);
console.log(`local  ${LOCAL_URL}\n`);
console.log(`tables   live=${live.tables.size}  local=${local.tables.size}`);
console.log(`rpcs     live=${live.rpcs.size}  local=${local.rpcs.size}\n`);

const missingTables = only(live.tables, local.tables);
const extraTables = only(local.tables, live.tables);
console.log(`tables missing locally : ${missingTables.length ? missingTables.join(", ") : "(none)"}`);
console.log(`tables extra locally   : ${extraTables.length ? extraTables.join(", ") : "(none)"}`);
if (missingTables.length) problems.push(`missing tables: ${missingTables.join(", ")}`);
if (extraTables.length) problems.push(`extra tables: ${extraTables.join(", ")}`);

const colDiffs = [];
for (const [table, liveCols] of live.tables) {
  const localCols = local.tables.get(table);
  if (!localCols) continue;
  for (const [col, type] of liveCols) {
    if (!localCols.has(col)) colDiffs.push(`${table}.${col} missing locally`);
    else if (localCols.get(col) !== type) {
      colDiffs.push(`${table}.${col} type live=${type} local=${localCols.get(col)}`);
    }
  }
  for (const col of localCols.keys()) {
    if (!liveCols.has(col)) colDiffs.push(`${table}.${col} extra locally`);
  }
}
console.log(`column diffs           : ${colDiffs.length ? "" : "(none)"}`);
for (const d of colDiffs) console.log(`    ${d}`);
if (colDiffs.length) problems.push(`${colDiffs.length} column diffs`);

const missingRpcs = only(live.rpcs, local.rpcs);
const extraRpcs = only(local.rpcs, live.rpcs);
const argDiffs = [];
for (const [name, args] of live.rpcs) {
  if (local.rpcs.has(name) && local.rpcs.get(name) !== args) {
    argDiffs.push(`${name}(live: ${args || "-"} | local: ${local.rpcs.get(name) || "-"})`);
  }
}
console.log(`rpcs missing locally   : ${missingRpcs.length ? missingRpcs.join(", ") : "(none)"}`);
console.log(`rpcs extra locally     : ${extraRpcs.length ? extraRpcs.join(", ") : "(none)"}`);
console.log(`rpc signature diffs    : ${argDiffs.length ? argDiffs.join(", ") : "(none)"}`);
if (missingRpcs.length) problems.push(`missing rpcs: ${missingRpcs.join(", ")}`);
if (extraRpcs.length) problems.push(`extra rpcs: ${extraRpcs.join(", ")}`);
if (argDiffs.length) problems.push(`rpc signature diffs: ${argDiffs.join(", ")}`);

if (problems.length) {
  console.log(`\nDRIFT: ${problems.join(" | ")}`);
} else {
  console.log("\nPARITY: local matches live across every REST-visible table, column and RPC.");
  console.log("        (indexes, constraints, triggers and RLS are NOT compared — see header.)");
}

// Set exitCode instead of calling process.exit(). fetch() leaves keep-alive
// sockets in undici's pool, and tearing the loop down under them crashes Node 24
// on Windows with `Assertion failed: !(handle->flags & UV_HANDLE_CLOSING)` and a
// bogus exit code 9 — which reads as a script bug rather than as detected drift.
// Letting the pool time out costs a few seconds and reports the real code.
process.exitCode = problems.length ? 1 : 0;

#!/usr/bin/env node
/**
 * Generates this install's config: config/.env, postgrest.conf, Caddyfile and the
 * three WinSW service definitions.
 *
 * Same contract as the Docker stack's bootstrap — fresh secrets per machine,
 * existing values preserved on a re-run, --rotate to deliberately mint new ones.
 * The key signing itself is shared (local-stack-shared/scripts/secrets.mjs) so
 * the two stacks produce interchangeable keys.
 *
 * Rotating requires re-running db-init.ps1 as well: the Postgres role passwords
 * are set once, at initdb time.
 */

import { readFileSync, writeFileSync, existsSync, mkdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { alnum, supabaseKey, renderEnvFile } from "../../local-stack-shared/scripts/secrets.mjs";
import { readEnvFile } from "../../local-stack-shared/scripts/env.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const PKG = join(HERE, "..");
const CONFIG = join(PKG, "config");
const TEMPLATES = join(PKG, "templates");

const rotate = process.argv.includes("--rotate");
const arg = (name, fallback) => {
  const i = process.argv.indexOf(name);
  return i === -1 ? fallback : process.argv[i + 1];
};

mkdirSync(CONFIG, { recursive: true });
mkdirSync(join(CONFIG, "services"), { recursive: true });

const ENV_PATH = join(CONFIG, ".env");
const existing = readEnvFile(ENV_PATH);
const generated = [];

function ensure(key, make) {
  if (!rotate && existing[key]) return existing[key];
  generated.push(key);
  return make();
}

const jwtSecret = ensure("JWT_SECRET", () => alnum(48));
const secretChanged = generated.includes("JWT_SECRET");
// If the secret changed the old keys no longer verify, so they must be reissued
// together — otherwise PostgREST rejects a key that looks perfectly valid.
const anonKey = secretChanged || !existing.ANON_KEY ? supabaseKey("anon", jwtSecret) : existing.ANON_KEY;
const serviceKey =
  secretChanged || !existing.SERVICE_ROLE_KEY ? supabaseKey("service_role", jwtSecret) : existing.SERVICE_ROLE_KEY;
if (anonKey !== existing.ANON_KEY && !generated.includes("ANON_KEY")) generated.push("ANON_KEY");
if (serviceKey !== existing.SERVICE_ROLE_KEY && !generated.includes("SERVICE_ROLE_KEY")) {
  generated.push("SERVICE_ROLE_KEY");
}

// Ports are design-v2's, and they are NOT the Docker stack's (54321/54322/54323)
// on purpose: both stacks can then be installed on the same machine without
// colliding, which is exactly the situation on the dev box.
const env = {
  PGDATA: resolve(arg("--data-dir", existing.PGDATA || join(PKG, "data"))),
  PGPORT: existing.PGPORT || "5432",
  POSTGRES_DB: existing.POSTGRES_DB || "postgres",
  POSTGRES_PASSWORD: ensure("POSTGRES_PASSWORD", () => alnum(32)),
  JWT_SECRET: jwtSecret,
  ANON_KEY: anonKey,
  SERVICE_ROLE_KEY: serviceKey,
  POSTGREST_PORT: existing.POSTGREST_PORT || "3000",
  // Read by local-stack-shared/scripts/env.mjs gatewayUrl(); this is what
  // SUPABASE_URL points at.
  GATEWAY_PORT: existing.GATEWAY_PORT || "8000",
  BACKEND_PORT: existing.BACKEND_PORT || "3001",
  BACKEND_API_KEY: ensure("BACKEND_API_KEY", () => alnum(32)),
  VENDOR_DIR: resolve(arg("--vendor-dir", existing.VENDOR_DIR || join(PKG, "vendor"))),
  // Where the built Express backend lives. Empty in the repo (the backend is run
  // from ..\backend by hand); set by the installer, which stages dist/ and
  // node_modules/ inside the install directory.
  BACKEND_DIR: arg("--backend-dir", existing.BACKEND_DIR || ""),
};

writeFileSync(
  ENV_PATH,
  renderEnvFile(env, "Regenerate with: node scripts/bootstrap.mjs --rotate (then re-run db-init.ps1)"),
  "utf8",
);

const PG_BIN = join(env.VENDOR_DIR, "pgsql", "bin");

function render(templateName, outPath, vars) {
  const src = join(TEMPLATES, templateName);
  if (!existsSync(src)) throw new Error(`missing template: ${src}`);
  let text = readFileSync(src, "utf8");
  for (const [k, v] of Object.entries(vars)) text = text.replaceAll(`\${${k}}`, v);
  const left = text.match(/\$\{[A-Z_]+\}/g);
  if (left) throw new Error(`unsubstituted placeholders in ${templateName}: ${[...new Set(left)].join(", ")}`);

  // Validate anything XML before writing it. WinSW silently refuses to install a
  // service whose config will not parse, and the most likely way to break these
  // is prose: a double hyphen inside an XML comment is illegal, and this repo's
  // house style uses one as an ASCII em-dash everywhere else. That mistake
  // produced four malformed service definitions once already.
  if (outPath.endsWith(".xml")) assertWellFormedXml(text, templateName);

  writeFileSync(outPath, text, "utf8");
  return outPath;
}

/**
 * Deliberately dependency-free: this runs before any npm install, from an
 * installer, on a machine with nothing on it. Checks the two things that
 * actually go wrong here rather than attempting a full parse.
 */
function assertWellFormedXml(text, label) {
  for (const match of text.matchAll(/<!--([\s\S]*?)-->/g)) {
    if (match[1].includes("--")) {
      const line = text.slice(0, match.index).split("\n").length;
      throw new Error(
        `${label}: XML comment near line ${line} contains "--", which is illegal in XML. ` +
        `Use a single hyphen or reword.`,
      );
    }
  }
  let depth = 0;
  for (const tag of text.matchAll(/<(\/?)([A-Za-z][\w.-]*)([^>]*?)(\/?)>/g)) {
    const [, closing, , , selfClosing] = tag;
    if (closing) depth -= 1;
    else if (!selfClosing) depth += 1;
    if (depth < 0) throw new Error(`${label}: closing tag with no opener near offset ${tag.index}`);
  }
  if (depth !== 0) throw new Error(`${label}: ${depth} unclosed element(s)`);
}

render("postgrest.conf.template", join(CONFIG, "postgrest.conf"), {
  POSTGRES_PASSWORD: env.POSTGRES_PASSWORD,
  PGPORT: env.PGPORT,
  POSTGRES_DB: env.POSTGRES_DB,
  JWT_SECRET: env.JWT_SECRET,
  POSTGREST_PORT: env.POSTGREST_PORT,
});

render("Caddyfile.template", join(CONFIG, "Caddyfile"), {
  GATEWAY_PORT: env.GATEWAY_PORT,
  POSTGREST_PORT: env.POSTGREST_PORT,
});

// ── WinSW service definitions ────────────────────────────────────────────────
// One XML per service. The PATH entry on the PostgREST service is not optional:
// postgrest.exe is not self-contained on Windows and dies with 0xC0000135
// (STATUS_DLL_NOT_FOUND), printing nothing at all, if libpq.dll is not on PATH.
// A service has none of the interactive shell's PATH, so it must be stated here.
const services = [
  {
    id: "tallybridge-postgres",
    name: "TallyBridge PostgreSQL",
    description: "PostgreSQL database holding the TallyBridge ledger.",
    // postgres.exe DIRECTLY, not `pg_ctl runservice` and not `pg_ctl start`.
    //
    //   pg_ctl runservice  calls StartServiceCtrlDispatcher and expects to have
    //                      been launched by the Windows SCM itself. Under WinSW
    //                      -- which is the process the SCM actually launched --
    //                      that handshake fails.
    //   pg_ctl start       forks the postmaster and exits immediately, so WinSW
    //                      sees its child die and marks the service stopped
    //                      while the database is in fact running, unmanaged.
    //
    // postgres.exe is an ordinary foreground console process, which is exactly
    // what a service wrapper wants.
    executable: join(PG_BIN, "postgres.exe"),
    arguments: `-D "${env.PGDATA}" -p ${env.PGPORT}`,
    // Stopping needs pg_ctl, though. WinSW's default stop is a console signal
    // then TerminateProcess, and the Windows postmaster does not shut down
    // cleanly on a console signal -- it would be killed mid-checkpoint and
    // recover from WAL on next boot, every time. `-m fast` rolls back open
    // transactions and checkpoints properly.
    stopExecutable: join(PG_BIN, "pg_ctl.exe"),
    stopArguments: `-D "${env.PGDATA}" -m fast -w stop`,
    path: PG_BIN,
  },
  {
    id: "tallybridge-postgrest",
    name: "TallyBridge PostgREST",
    description: "Serves the TallyBridge database as a REST API on 127.0.0.1.",
    executable: join(env.VENDOR_DIR, "postgrest", "postgrest.exe"),
    arguments: `"${join(CONFIG, "postgrest.conf")}"`,
    path: PG_BIN,
    depends: "tallybridge-postgres",
  },
  {
    id: "tallybridge-gateway",
    name: "TallyBridge API gateway",
    description: "Caddy. Maps /rest/v1/* onto PostgREST so supabase-js works unchanged.",
    executable: join(env.VENDOR_DIR, "caddy", "caddy.exe"),
    arguments: `run --config "${join(CONFIG, "Caddyfile")}" --adapter caddyfile`,
    path: "",
    depends: "tallybridge-postgrest",
  },
];

// The Express backend, when the installer has staged it. Reports are served by
// this, not by PostgREST directly, so without it the client's stack is running
// but every report is unreachable.
//
// The env block is the whole use-native.ps1 contract, restated for a service:
// BOTH Supabase clients must be set, and SUPABASE_URL_Client has a CAPITAL C
// (sync.ts:20). Set only the lowercase pair and the MRP report silently falls
// back to the global client — which here points at the same place, so it would
// still work; on a machine where the two ever differ it would not.
//
// FIREBASE_SERVICE_ACCOUNT_B64 is deliberately absent. db/firebase.ts initialises
// lazily, so an API-key-only deployment boots fine without it, and no Google
// service-account private key needs to travel to the client's PC.
if (env.BACKEND_DIR) {
  const gateway = `http://127.0.0.1:${env.GATEWAY_PORT}`;
  services.push({
    id: "tallybridge-backend",
    name: "TallyBridge API",
    description: "Express backend serving the MRP and stock reports.",
    executable: join(env.VENDOR_DIR, "node", "node.exe"),
    arguments: `"${join(env.BACKEND_DIR, "dist", "index.js")}"`,
    path: "",
    depends: "tallybridge-gateway",
    workingdirectory: env.BACKEND_DIR,
    envs: {
      SUPABASE_URL: gateway,
      SUPABASE_SERVICE_KEY: env.SERVICE_ROLE_KEY,
      API_KEY: env.BACKEND_API_KEY,
      SUPABASE_URL_Client: gateway,
      SUPABASE_SERVICE_KEY_CLIENT: env.SERVICE_ROLE_KEY,
      API_KEY_CLIENT: env.BACKEND_API_KEY,
      PORT: env.BACKEND_PORT,
      NODE_ENV: "production",
    },
  });
}

const xmlEscape = (s) =>
  String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

for (const s of services) {
  const envLines = [];
  if (s.path) envLines.push(`<env name="PATH" value="${xmlEscape(s.path)};%PATH%" />`);
  for (const [k, v] of Object.entries(s.envs || {})) {
    envLines.push(`<env name="${k}" value="${xmlEscape(v)}" />`);
  }
  const stop = s.stopExecutable
    ? `<stopexecutable>${xmlEscape(s.stopExecutable)}</stopexecutable>\n  <stoparguments>${xmlEscape(s.stopArguments)}</stoparguments>`
    : "";
  render("winsw.xml.template", join(CONFIG, "services", `${s.id}.xml`), {
    ID: s.id,
    NAME: s.name,
    DESCRIPTION: s.description,
    EXECUTABLE: s.executable,
    ARGUMENTS: s.arguments,
    STOP: stop,
    LOGPATH: join(PKG, "logs"),
    EXTRA_PATH: envLines.join("\n  "),
    DEPENDS: s.depends ? `<depend>${s.depends}</depend>` : "",
    WORKINGDIR: s.workingdirectory ? `<workingdirectory>${xmlEscape(s.workingdirectory)}</workingdirectory>` : "",
  });
}
mkdirSync(join(PKG, "logs"), { recursive: true });

console.log(`config     ${CONFIG}`);
console.log(`  .env             ${env.PGDATA ? "ok" : ""}  (data dir: ${env.PGDATA})`);
console.log(`  postgrest.conf   127.0.0.1:${env.POSTGREST_PORT} -> postgres:${env.PGPORT}`);
console.log(`  Caddyfile        127.0.0.1:${env.GATEWAY_PORT}/rest/v1/* -> :${env.POSTGREST_PORT}`);
console.log(`  services/        ${services.map((s) => s.id).join(", ")}`);
console.log(generated.length ? `generated: ${generated.join(", ")}` : "generated: nothing (all values already present)");

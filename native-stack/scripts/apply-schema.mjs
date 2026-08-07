#!/usr/bin/env node
/**
 * Applies the schema to the native Postgres, in dependency order.
 *
 * Same file list as the Docker stack — imported from
 * local-stack-shared/scripts/schema-sources.mjs, which is the single source of
 * truth and carries the explanation of the three migration defects it works
 * around. The only thing that differs here is how psql is reached: a real
 * psql.exe from vendor\pgsql\bin instead of `docker compose exec db psql`.
 */

import { spawnSync } from "node:child_process";
import { readFileSync, existsSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { collectSchemaFiles, collectBundledFiles } from "../../local-stack-shared/scripts/schema-sources.mjs";
import { readEnvFile } from "../../local-stack-shared/scripts/env.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const PKG = join(HERE, "..");
const REPO = join(PKG, "..");
const BUNDLED = join(PKG, "schema", "bundled");

const env = readEnvFile(join(PKG, "config", ".env"));
if (!env.PGDATA) {
  console.error("config/.env not found — run: node scripts/bootstrap.mjs");
  process.exit(2);
}

const PSQL = join(env.VENDOR_DIR, "pgsql", "bin", "psql.exe");
if (!existsSync(PSQL)) {
  console.error(`psql.exe not found at ${PSQL} — run: .\\fetch-vendor.ps1 -IncludePostgres`);
  process.exit(2);
}

function collectFiles() {
  const bundled = collectBundledFiles(BUNDLED);
  if (bundled) return { mode: "bundled", files: bundled };
  return { mode: "dev", files: collectSchemaFiles(REPO) };
}

function psql(sql, label) {
  const result = spawnSync(
    PSQL,
    [
      "-h", "127.0.0.1",
      "-p", env.PGPORT,
      "-U", "postgres",
      "-d", env.POSTGRES_DB || "postgres",
      // Without ON_ERROR_STOP psql reports success having skipped every
      // statement after the first failure.
      "-v", "ON_ERROR_STOP=1",
      "--quiet", "--no-psqlrc",
    ],
    {
      input: sql,
      encoding: "utf8",
      env: { ...process.env, PGPASSWORD: env.POSTGRES_PASSWORD },
    },
  );
  if (result.status !== 0) {
    console.error(`\nFAILED: ${label}`);
    console.error((result.stderr || result.stdout || "").trim());
    process.exit(1);
  }
  return (result.stdout || "").trim();
}

const { mode, files } = collectFiles();
console.log(`applying ${files.length} SQL files (${mode} layout) to 127.0.0.1:${env.PGPORT}\n`);

for (const file of files) {
  const label = relative(REPO, file).replace(/\\/g, "/");
  // Strip a UTF-8 BOM. PowerShell 5.1's Set-Content -Encoding utf8 writes one and
  // Postgres rejects the leading U+FEFF as a syntax error on line 1 — an error
  // that points nowhere near the cause.
  const sql = readFileSync(file, "utf8").replace(/^﻿/, "");
  psql(sql, label);
  console.log(`  ok  ${label}`);
}

// PostgREST caches the schema at connect time and only learns about DDL through
// the `pgrst` NOTIFY, which 902_grants.sql issues — but that reaches nothing if
// PostgREST was not connected and listening when it fired.
console.log("\nIf PostgREST is already running, restart it (or it may 404 new tables):");
console.log("  .\\stop.ps1 ; .\\start.ps1");
console.log(`\ndone — ${files.length} files applied.`);

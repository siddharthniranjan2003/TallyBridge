#!/usr/bin/env node
/**
 * Applies the full schema to the local Postgres, in dependency order.
 *
 * Runs every file through `docker compose exec -T db psql`, so it needs no psql
 * on the host and no npm dependencies — the only prerequisite is the same Docker
 * the stack already requires.
 *
 * ── Why this exists instead of `supabase db reset` ───────────────────────────
 *
 * `supabase/migrations/` cannot build a database on its own, and three separate
 * defects in it break the CLI's own reset:
 *
 *   1. No base schema. 20260619_stock_items_part_code.sql runs
 *      `ALTER TABLE public.stock_items` on a table that no migration creates —
 *      the base tables live in backend/full_schema.sql plus three loose files.
 *      Handled here by the `base` stage below.
 *
 *   2. Duplicate migration versions. The CLI keys schema_migrations on the digits
 *      before the first '_', and three files all parse to 20260619, so a reset
 *      dies mid-run on a duplicate-key violation and leaves a half-applied DB.
 *      Sidestepped here: this script orders by full filename and tracks nothing,
 *      so same-date files are simply applied in name order. (The three do not
 *      depend on one another.)
 *
 *   3. Rollback scripts sort AFTER their forward migration, so a reset applies
 *      both and silently undoes the work — including the stock-unit guard and the
 *      global-latest-rate RPCs. Excluded here by the `_rollback.sql` filter, which
 *      is enforced in code rather than left to whoever maintains a list.
 *
 * Every file is idempotent (CREATE ... IF NOT EXISTS / CREATE OR REPLACE), so
 * re-running this on a populated database is safe and is the intended way to pick
 * up a new migration. It does not drop anything.
 */

import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { collectSchemaFiles, collectBundledFiles } from "../../local-stack-shared/scripts/schema-sources.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const PKG = join(HERE, "..");
const REPO = join(PKG, "..");
const BUNDLED = join(PKG, "schema", "bundled");

/**
 * Two layouts are supported:
 *   dev     — read straight out of the repo, so there is one copy of each file
 *             and no chance of the package drifting from the source of truth.
 *   bundled — a flat, numbered schema/bundled/ produced by build-client-package.ps1
 *             for the standalone client deliverable, which ships without backend/
 *             or supabase/.
 *
 * The ordering itself lives in local-stack-shared/scripts/schema-sources.mjs so
 * that this stack and the native Windows stack cannot drift apart — see the long
 * comment there for the three migration defects it works around.
 */
function collectFiles() {
  const bundled = collectBundledFiles(BUNDLED);
  if (bundled) return { mode: "bundled", files: bundled };
  return { mode: "dev", files: collectSchemaFiles(REPO) };
}

function psql(sql, label) {
  const result = spawnSync(
    "docker",
    [
      "compose", "exec", "-T", "db",
      "psql", "-U", "postgres", "-d", process.env.POSTGRES_DB || "postgres",
      // ON_ERROR_STOP is the whole point: without it psql reports success while
      // having skipped every statement after the first failure.
      "-v", "ON_ERROR_STOP=1",
      "--quiet", "--no-psqlrc",
    ],
    { cwd: PKG, input: sql, encoding: "utf8" },
  );
  if (result.status !== 0) {
    console.error(`\nFAILED: ${label}`);
    console.error((result.stderr || result.stdout || "").trim());
    process.exit(1);
  }
  return (result.stdout || "").trim();
}

const { mode, files } = collectFiles();
console.log(`applying ${files.length} SQL files (${mode} layout)\n`);

let applied = 0;
for (const file of files) {
  const label = relative(REPO, file).replace(/\\/g, "/");
  // Strip a UTF-8 BOM if one is present. PowerShell 5.1's `Set-Content -Encoding
  // utf8` writes one, and Postgres rejects the leading U+FEFF as a syntax error
  // on line 1 — an error message that gives no hint of the real cause.
  const sql = readFileSync(file, "utf8").replace(/^﻿/, "");
  const out = psql(sql, label);
  applied += 1;
  const notices = out
    .split("\n")
    .filter((l) => /^(NOTICE|WARNING)/.test(l) && !/already exists|skipping/.test(l));
  console.log(`  ok  ${label}`);
  for (const n of notices) console.log(`      ${n}`);
}

// PostgREST caches the schema at connect time. 902_grants.sql issues a NOTIFY,
// but that only helps if the listener was already connected; a container that
// booted before the tables existed can sit on an empty cache and 404 everything.
// Restarting it is the one reliable answer and costs about a second.
console.log("\nreloading PostgREST schema cache...");
spawnSync("docker", ["compose", "restart", "rest"], { cwd: PKG, stdio: "ignore" });

console.log(`\ndone — ${applied} files applied.`);

/**
 * THE ordered list of SQL files that builds the database. Single source of truth
 * for both stacks and both client packagers.
 *
 * It lives here, on its own, because the failure it prevents is silent: the
 * Docker stack and the native stack are two deployments of one schema, and if
 * each kept its own list, adding a migration to one would leave the other quietly
 * a version behind. Every consumer imports this — apply-schema in both stacks and
 * both build-client-package scripts.
 *
 * ── Why the repo's own migrations directory is not enough ────────────────────
 *
 * `supabase/migrations/` cannot build a database on its own, and three defects in
 * it break `supabase db reset` on any machine:
 *
 *   1. No base schema. 20260619_stock_items_part_code.sql runs
 *      `ALTER TABLE public.stock_items` on a table no migration creates — the base
 *      tables are in backend/full_schema.sql plus three loose files. Handled by
 *      the `base` stage below.
 *
 *   2. Duplicate migration versions. The CLI keys schema_migrations on the digits
 *      before the first '_', and three files all parse to 20260619, so a reset
 *      dies mid-run on a duplicate-key violation. Sidestepped: this orders by full
 *      filename and tracks nothing, so same-date files just apply in name order.
 *      (The three do not depend on one another.)
 *
 *   3. Rollback scripts sort AFTER their forward migration, so a reset applies
 *      both and silently undoes the work — including the stock-unit guard and the
 *      global-latest-rate RPCs. Excluded by the filter below, in code rather than
 *      by a hand-maintained list.
 */

import { readdirSync, existsSync } from "node:fs";
import { join } from "node:path";

/**
 * Superseded historical snapshots that must NOT be applied. supabase_schema_v2..v6
 * and supabase_new_tables.sql are older shapes of the same tables, and
 * supabase_push_queue_edit_state.sql is already folded into full_schema.sql.
 * Applying any of them produces a schema that no longer matches live.
 */
const BASE_FILES = [
  "full_schema.sql",
  "supabase_scan_jobs.sql",
  "supabase_scan_jobs_failed_status.sql",
  "supabase_audit_trail_purchase.sql",
];

export const ROLLBACK_SUFFIX = "_rollback.sql";

/**
 * @param repoRoot   the TallyBridge checkout
 * @returns absolute paths, in apply order
 */
export function collectSchemaFiles(repoRoot) {
  const base = BASE_FILES.map((f) => join(repoRoot, "backend", f));

  const migrationsDir = join(repoRoot, "supabase", "migrations");
  const migrations = readdirSync(migrationsDir)
    .filter((f) => f.endsWith(".sql"))
    .filter((f) => !f.endsWith(ROLLBACK_SUFFIX)) // defect 3
    .sort()
    .map((f) => join(migrationsDir, f));

  const postDir = join(repoRoot, "local-stack-shared", "schema", "90-post");
  const post = readdirSync(postDir)
    .filter((f) => f.endsWith(".sql"))
    .sort()
    .map((f) => join(postDir, f));

  return [...base, ...migrations, ...post];
}

/**
 * A packaged stack ships a flat, numbered copy of the above because it has no
 * backend/ or supabase/ next to it. Callers prefer this when present.
 */
export function collectBundledFiles(bundledDir) {
  if (!existsSync(bundledDir)) return null;
  return readdirSync(bundledDir)
    .filter((f) => f.endsWith(".sql"))
    .sort()
    .map((f) => join(bundledDir, f));
}

/**
 * Shared env-file reading for both local stacks.
 *
 * There are two deployments of the same database and they keep their generated
 * secrets in different places:
 *
 *   local-supabase/.env         Docker stack (dev box, has Studio)
 *   native-stack/config/.env    native Windows services (client PC, no Docker)
 *
 * Scripts that work against either one resolve the file through TB_ENV_FILE
 * rather than assuming a location, so a single copy of copy-live-to-local.mjs and
 * schema-parity.mjs serves both. Without that the two stacks would each need
 * their own fork of those scripts, and the forks would drift the first time a
 * table was added.
 */

import { readFileSync, existsSync } from "node:fs";

export function readEnvFile(path) {
  if (!path || !existsSync(path)) return {};
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

/**
 * Resolve the stack's .env. TB_ENV_FILE wins; otherwise fall back to the
 * candidates the caller offers, in order.
 */
export function resolveStackEnv(...candidates) {
  const chosen = [process.env.TB_ENV_FILE, ...candidates].find((p) => p && existsSync(p));
  return { path: chosen || null, values: readEnvFile(chosen) };
}

/**
 * Base URL of whatever is serving /rest/v1 for this stack.
 *
 * Docker  -> Kong          on KONG_HTTP_PORT (54321)
 * Native  -> Caddy         on GATEWAY_PORT   (8000)
 *
 * Both terminate at PostgREST and both answer on the same paths, which is the
 * whole point: SUPABASE_URL is the only thing that differs between the two
 * deployments, and no application code knows which one it is talking to.
 */
export function gatewayUrl(env) {
  const port = env.GATEWAY_PORT || env.KONG_HTTP_PORT || "54321";
  return `http://127.0.0.1:${port}`;
}

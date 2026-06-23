import https from "https";
import os from "os";
import { app } from "electron";

/**
 * Remote log forwarder -> BetterStack (Logtail) Live Tail.
 *
 * Taps the same `sync-log` lines that fill the in-app Sync Log panel and ships
 * them, batched, to BetterStack so a client's panel can be watched live (1-3s
 * behind) from the BetterStack dashboard.
 *
 * Config: env vars override the baked-in defaults below.
 *   TB_LOGTAIL_TOKEN  - BetterStack source token (write-only, safe to ship)
 *   TB_LOGTAIL_HOST   - the source's ingesting host, e.g.
 *                       s1234567.xx-xxx-1.betterstackdata.com
 *
 * The defaults are the `tallybridge-client` source so release builds forward
 * logs without any extra config. The token is write-only ingest (Sentry-DSN
 * model) — safe to ship. Empty either value (env set to "") to disable.
 */

const DEFAULT_TOKEN = "cF8wGJs85zu8wLzjVaXGGq7e";
const DEFAULT_HOST = "s2537912.eu-fsn-3.betterstackdata.com";

const TOKEN = (process.env.TB_LOGTAIL_TOKEN ?? DEFAULT_TOKEN).trim();
const HOST = (process.env.TB_LOGTAIL_HOST ?? DEFAULT_HOST).trim();
const ENABLED = Boolean(TOKEN && HOST);

const FLUSH_INTERVAL_MS = 2000;
const MAX_BATCH = 50;   // flush early once this many lines are buffered
const MAX_BUFFER = 1000; // hard cap so a chatty full-sync can't grow memory

type LogRecord = {
  dt: string;
  message: string;
  level: string;
  company?: string;
  tally_guid?: string;
  company_local_id?: string;
  ingest_host?: string;
  ingest_mode?: string;
  app_version: string;
  device: string;
  platform: string;
};

let buffer: LogRecord[] = [];
let timer: NodeJS.Timeout | null = null;
let appVersion = "";
let device = "";
let started = false;

// Identity of the company whose sync is currently running, so each log line
// can be filtered unambiguously (e.g. tally_guid) instead of by a name that
// can collide across testers. Set by the sync engine at each run start.
let context: {
  company?: string;
  tally_guid?: string;
  company_local_id?: string;
  ingest_host?: string;
  ingest_mode?: string;
} = {};

/** Set the active company identity for subsequent log lines. */
export function setLogContext(ctx: {
  company?: string;
  tally_guid?: string;
  company_local_id?: string;
  ingest_host?: string;
  ingest_mode?: string;
}) {
  context = ctx || {};
}

function ensureStarted() {
  if (started || !ENABLED) return;
  started = true;
  try {
    appVersion = app.getVersion();
  } catch {
    appVersion = "";
  }
  device = os.hostname();
  timer = setInterval(flush, FLUSH_INTERVAL_MS);
  // don't let the flush timer keep the process alive
  if (timer.unref) timer.unref();
  app.on("before-quit", flush);
}

/**
 * Forward one Sync Log line to BetterStack. No-op unless configured.
 * Fully wrapped so logging can NEVER throw into the sync/IPC call sites.
 */
export function shipSyncLog(data: { company?: string; line?: string } | undefined) {
  if (!ENABLED) return;
  try {
    const line = (data?.line ?? "").toString().trim();
    if (!line) return;
    ensureStarted();

    const level = /\[err|error/i.test(line) ? "error" : "info";
    // Attach the unique company identity only when this line belongs to the
    // active company run, so generic "System"/"Push API" lines aren't mislabeled.
    const matchesContext = !!data?.company && data.company === context.company;
    buffer.push({
      dt: new Date().toISOString(),
      message: line,
      level,
      company: data?.company,
      tally_guid: matchesContext ? context.tally_guid : undefined,
      company_local_id: matchesContext ? context.company_local_id : undefined,
      ingest_host: matchesContext ? context.ingest_host : undefined,
      ingest_mode: matchesContext ? context.ingest_mode : undefined,
      app_version: appVersion,
      device,
      platform: process.platform,
    });

    if (buffer.length >= MAX_BATCH) flush();
    if (buffer.length > MAX_BUFFER) buffer = buffer.slice(-MAX_BUFFER);
  } catch {
    // logging must never break a sync — drop silently
  }
}

function flush() {
  if (!ENABLED || buffer.length === 0) return;
  // Wrapped so a throw here can never escape into the setInterval/before-quit
  // callback and crash the main process.
  try {
    const batch = buffer;
    buffer = [];

    const body = JSON.stringify(batch);
    const req = https.request(
      {
        method: "POST",
        host: HOST,
        path: "/",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${TOKEN}`,
          "Content-Length": Buffer.byteLength(body),
        },
      },
      (res) => {
        // drain so the socket can be reused; we don't act on the response
        res.resume();
      },
    );
    // Never let logging break a sync: on any transport error, drop this batch.
    req.on("error", () => {});
    req.write(body);
    req.end();
  } catch {
    // drop the batch; never throw out of a timer callback
  }
}

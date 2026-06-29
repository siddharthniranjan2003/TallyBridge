// Sentry initialization. This module MUST be imported before any other module
// (it is the first import in index.ts) so the SDK can instrument http/express
// before they are loaded. For full ESM auto-instrumentation you may instead run
// the process with `node --import ./dist/instrument.js dist/index.js`.
//
// dotenv is loaded here (not just in index.ts) because Sentry.init reads
// SENTRY_DSN from process.env at module-evaluation time, which happens before
// index.ts runs its own dotenv.config(). On Cloud Run the env comes from the
// runtime, so the .env file is a local-dev convenience.
import * as Sentry from "@sentry/node";
import dotenv from "dotenv";
dotenv.config();

const dsn = process.env.SENTRY_DSN?.trim();

Sentry.init({
  // No DSN => the SDK is a complete no-op, so unconfigured builds behave exactly
  // as before. Mirrors the BetterStack token model already used by the desktop app.
  dsn: dsn || undefined,
  environment:
    process.env.SENTRY_ENVIRONMENT?.trim() ||
    process.env.NODE_ENV ||
    "development",
  release: process.env.SENTRY_RELEASE?.trim() || undefined,
  // Distributed tracing: continues the trace started by the Flutter app (via the
  // incoming sentry-trace/baggage headers) so a failed in-app call links to the
  // backend exception as one trace. Tune down in high-volume prod if needed.
  tracesSampleRate: Number(process.env.SENTRY_TRACES_SAMPLE_RATE ?? "1.0"),
  // uid is set explicitly in auth middleware; do not auto-attach IPs/headers/PII.
  sendDefaultPii: false,
});

if (!dsn) {
  console.warn(
    "[Sentry] SENTRY_DSN not set — backend error reporting disabled (no-op).",
  );
}

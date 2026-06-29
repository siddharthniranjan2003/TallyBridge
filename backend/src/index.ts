// Must be first: initializes Sentry before http/express are imported.
import "./instrument.js";

import express from "express";
import cors from "cors";
import dotenv from "dotenv";
import * as Sentry from "@sentry/node";
import { randomUUID } from "node:crypto";
dotenv.config();

import syncRouter from "./routes/sync.js";
import pushVoucherRouter from "./routes/push-voucher.js";
import pushInvoiceRouter from "./routes/push-invoice.js";

function resolveJsonBodyLimit() {
  const configured = process.env.TB_JSON_BODY_LIMIT?.trim();
  return configured || "100mb";
}

const app = express();
app.use(cors());
app.use(express.json({ limit: resolveJsonBodyLimit() }));

// Correlation: accept the client-generated request id (the Flutter app sends one
// per backend call), or mint one. Echo it back and pin it to the Sentry isolation
// scope so every event captured while handling this request carries request_id —
// greppable in Cloud Run logs and linkable to the in-app error.
app.use((req, res, next) => {
  const headerVal = req.headers["x-request-id"];
  const requestId =
    (Array.isArray(headerVal) ? headerVal[0] : headerVal)?.trim() || randomUUID();
  res.setHeader("x-request-id", requestId);
  Sentry.getIsolationScope().setTag("request_id", requestId);
  next();
});

app.use("/api/sync", syncRouter);
app.use("/api/push-voucher", pushVoucherRouter);
app.use("/api/push-invoice", pushInvoiceRouter);

app.get("/health", (_, res) => {
  res.json({ status: "ok", service: "TallyBridge API" });
});

// TEMPORARY — Sentry smoke test. `curl https://<service>/debug-sentry` throws an
// uncaught error so the Sentry error handler captures it. Delete after verifying.
app.get("/debug-sentry", () => {
  throw new Error("Sentry verify: backend test error");
});

// Captures any error thrown by the routes above (must come after them). No-op
// reporting when SENTRY_DSN is unset; the request still fails as it did before.
Sentry.setupExpressErrorHandler(app);

process.on('unhandledRejection', (reason) => {
  console.error('[TallyBridge API] Unhandled rejection:', reason);
});
process.on('uncaughtException', (err) => {
  console.error('[TallyBridge API] Uncaught exception:', err);
});

const PORT = process.env.PORT || 3001;
app.listen(PORT, () => {
  console.log(`[TallyBridge API] Running on port ${PORT}`);
});

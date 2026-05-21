import express from "express";
import cors from "cors";
import dotenv from "dotenv";
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

app.use("/api/sync", syncRouter);
app.use("/api/push-voucher", pushVoucherRouter);
app.use("/api/push-invoice", pushInvoiceRouter);

app.get("/health", (_, res) => {
  res.json({ status: "ok", service: "TallyBridge API" });
});

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

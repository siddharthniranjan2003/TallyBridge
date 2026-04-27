import { Router } from "express";
import { request as httpRequest } from "node:http";
import { requireApiKey } from "../middleware/auth.js";

const router = Router();

const GST_RATE = 0.09;

function resolveLocalPushUrl() {
  return (process.env.TALLYBRIDGE_LOCAL_PUSH_URL || "http://127.0.0.1:3002/push-voucher").trim();
}

function todayTallyDate() {
  const d = new Date();
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}

function round2(n: number) {
  return Math.round(n * 100) / 100;
}

function postJson(urlText: string, payload: unknown) {
  return new Promise<{ statusCode: number; body: string }>((resolve, reject) => {
    const target = new URL(urlText);
    const body = JSON.stringify(payload);
    const req = httpRequest(
      {
        hostname: target.hostname,
        port: target.port,
        path: `${target.pathname}${target.search}`,
        method: "POST",
        headers: {
          "content-type": "application/json",
          "content-length": Buffer.byteLength(body).toString(),
        },
      },
      (res) => {
        const chunks: Buffer[] = [];
        res.on("data", (chunk) => chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)));
        res.on("end", () =>
          resolve({ statusCode: res.statusCode || 502, body: Buffer.concat(chunks).toString("utf8") }),
        );
      },
    );
    req.on("error", reject);
    req.write(body);
    req.end();
  });
}

interface InvoiceItem {
  stock_item_name: string;
  quantity: number;
  rate: number;
  amount: number;
  unit?: string;
  godown_name?: string;
}

interface PushInvoicePayload {
  party_name: string;
  items: InvoiceItem[];
  date?: string;
  voucher_number?: string;
  reference?: string;
  narration?: string;
  company_name?: string;
}

function buildVoucher(payload: PushInvoicePayload) {
  const { party_name, items, company_name } = payload;
  const date = payload.date || todayTallyDate();
  const voucher_number = payload.voucher_number || `INV-${Date.now()}`;
  const reference = payload.reference || voucher_number;
  const narration = payload.narration || "Sales invoice";

  const subtotal = round2(items.reduce((sum, item) => sum + item.amount, 0));
  const cgst = round2(subtotal * GST_RATE);
  const sgst = round2(subtotal * GST_RATE);
  const total = round2(subtotal + cgst + sgst);

  return {
    date,
    voucher_type: "GST SALE",
    voucher_number,
    party_name,
    narration,
    reference,
    inventory_ledger_name: "GST SALE",
    company_name,
    ledger_entries: [
      { ledger_name: party_name, amount: total,    is_deemed_positive: true  },
      { ledger_name: "GST SALE",  amount: subtotal, is_deemed_positive: false },
      { ledger_name: "CGST",      amount: cgst,     is_deemed_positive: false },
      { ledger_name: "SGST",      amount: sgst,     is_deemed_positive: false },
    ],
    items: items.map((item) => ({
      stock_item_name: item.stock_item_name,
      quantity:        item.quantity,
      unit:            item.unit || "NOS",
      rate:            item.rate,
      amount:          item.amount,
      godown_name:     item.godown_name || "Main Location",
    })),
  };
}

router.post("/", requireApiKey, async (req, res) => {
  const payload = req.body as PushInvoicePayload;

  if (!payload?.party_name || !Array.isArray(payload.items) || payload.items.length === 0) {
    return res.status(400).json({
      ok: false,
      error: "Payload must include party_name and a non-empty items array",
    });
  }

  try {
    const voucher = buildVoucher(payload);
    const forwarded = await postJson(resolveLocalPushUrl(), voucher);
    let parsed: unknown;
    try {
      parsed = JSON.parse(forwarded.body);
    } catch {
      parsed = { ok: false, error: "TallyBridge returned a non-JSON response", raw: forwarded.body };
    }
    res.status(forwarded.statusCode).json(parsed);
  } catch (error) {
    const message = error instanceof Error ? error.message : "Unknown error";
    res.status(502).json({ ok: false, error: `Could not reach local TallyBridge service: ${message}` });
  }
});

export default router;

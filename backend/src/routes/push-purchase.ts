import { Router } from "express";
import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";

import { requireApiKey } from "../middleware/auth.js";

const router = Router();

const DEFAULT_CGST_RATE = 0.09;
const DEFAULT_SGST_RATE = 0.09;
const DEFAULT_IGST_RATE = 0.18;

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
    const requestFn = target.protocol === "https:" ? httpsRequest : httpRequest;
    const req = requestFn(
      {
        protocol: target.protocol,
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

interface PurchaseItem {
  stock_item_name: string;
  quantity: number;
  rate: number;
  amount: number;
  unit?: string;
  godown_name?: string;
}

interface LedgerInput {
  ledger_name: string;
  amount: number;
  is_deemed_positive?: boolean;
}

interface PushPurchasePayload {
  party_name: string;
  items: PurchaseItem[];
  date?: string;
  voucher_number?: string;
  reference?: string;
  narration?: string;
  company_name?: string;
  voucher_type?: string;
  inventory_ledger_name?: string;
  purchase_ledger_name?: string;
  subtotal?: number;
  total?: number;
  tax_entries?: LedgerInput[];
  additional_ledger_entries?: LedgerInput[];
  interstate?: boolean;
  gst_mode?: "IGST" | "CGST_SGST" | "NONE";
  cgst_rate?: number;
  sgst_rate?: number;
  igst_rate?: number;
}

function normalizeLedgerInputs(entries: LedgerInput[] | undefined, defaultIsDeemedPositive: boolean) {
  if (!entries?.length) {
    return [];
  }

  return entries
    .map((entry) => ({
      ledger_name: String(entry.ledger_name || "").trim(),
      amount: round2(Number(entry.amount || 0)),
      is_deemed_positive: entry.is_deemed_positive ?? defaultIsDeemedPositive,
    }))
    .filter((entry) => entry.ledger_name && entry.amount > 0);
}

function deriveTaxEntries(payload: PushPurchasePayload, subtotal: number) {
  const explicit = normalizeLedgerInputs(payload.tax_entries, true);
  if (explicit.length) {
    return explicit;
  }

  const gstMode = payload.gst_mode || (payload.interstate ? "IGST" : "CGST_SGST");
  if (gstMode === "NONE") {
    return [];
  }

  if (gstMode === "IGST") {
    return [
      {
        ledger_name: "IGST",
        amount: round2(subtotal * (payload.igst_rate ?? DEFAULT_IGST_RATE)),
        is_deemed_positive: true,
      },
    ];
  }

  return [
    {
      ledger_name: "CGST",
      amount: round2(subtotal * (payload.cgst_rate ?? DEFAULT_CGST_RATE)),
      is_deemed_positive: true,
    },
    {
      ledger_name: "SGST",
      amount: round2(subtotal * (payload.sgst_rate ?? DEFAULT_SGST_RATE)),
      is_deemed_positive: true,
    },
  ].filter((entry) => entry.amount > 0);
}

function computePartyAmount(subtotal: number, taxEntries: ReturnType<typeof deriveTaxEntries>, extraEntries: LedgerInput[]) {
  const taxTotal = taxEntries.reduce((sum, entry) => sum + entry.amount, 0);
  const extraContribution = extraEntries.reduce(
    (sum, entry) => sum + (entry.is_deemed_positive ? entry.amount : -entry.amount),
    0,
  );
  return round2(subtotal + taxTotal + extraContribution);
}

function buildVoucher(payload: PushPurchasePayload) {
  const { party_name, items, company_name } = payload;
  const date = payload.date || todayTallyDate();
  const voucher_number = payload.voucher_number || `PUR-${Date.now()}`;
  const reference = payload.reference || voucher_number;
  const narration = payload.narration || "Purchase invoice";
  const voucher_type = String(payload.voucher_type || "GST PURCHASE").trim() || "GST PURCHASE";
  const inventory_ledger_name = String(
    payload.inventory_ledger_name || payload.purchase_ledger_name || voucher_type,
  ).trim();

  const normalizedItems = items.map((item) => ({
    stock_item_name: item.stock_item_name,
    quantity: item.quantity,
    unit: item.unit || "NOS",
    rate: item.rate,
    amount: item.amount,
    godown_name: item.godown_name || "Main Location",
  }));

  const subtotal = round2(payload.subtotal ?? normalizedItems.reduce((sum, item) => sum + item.amount, 0));
  const taxEntries = deriveTaxEntries(payload, subtotal);
  const additionalLedgerEntries = normalizeLedgerInputs(payload.additional_ledger_entries, true);
  const total = round2(payload.total ?? computePartyAmount(subtotal, taxEntries, additionalLedgerEntries));

  return {
    date,
    voucher_type,
    voucher_number,
    party_name,
    narration,
    reference,
    inventory_ledger_name,
    company_name,
    ledger_entries: [
      { ledger_name: party_name, amount: total, is_deemed_positive: false },
      { ledger_name: inventory_ledger_name, amount: subtotal, is_deemed_positive: true },
      ...taxEntries,
      ...additionalLedgerEntries,
    ],
    items: normalizedItems,
  };
}

router.post("/", requireApiKey, async (req, res) => {
  const payload = req.body as PushPurchasePayload;

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

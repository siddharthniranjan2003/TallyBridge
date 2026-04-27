import { serve } from "https://deno.land/std@0.224.0/http/server.ts";

import { INGEST_SKELETON_PHASE, SYNC_CONTRACT_VERSION } from "./contract.ts";

type SyncPayload = {
  company_name?: unknown;
  sync_contract_version?: unknown;
  company_info?: unknown;
  alter_ids?: unknown;
  groups?: unknown;
  ledgers?: unknown;
  vouchers?: unknown;
  stock_items?: unknown;
  outstanding?: unknown;
  profit_loss?: unknown;
  balance_sheet?: unknown;
  trial_balance?: unknown;
  sync_meta?: unknown;
};

const corsHeaders = {
  "access-control-allow-origin": "*",
  "access-control-allow-headers": "authorization, x-client-info, apikey, content-type, x-sync-key, x-sync-contract-version, x-sync-dry-run",
  "access-control-allow-methods": "GET,POST,OPTIONS",
};

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      ...corsHeaders,
      "content-type": "application/json; charset=utf-8",
    },
  });
}

function countRows(value: unknown) {
  return Array.isArray(value) ? value.length : 0;
}

function summarizeRecords(payload: SyncPayload) {
  return {
    groups: countRows(payload.groups),
    ledgers: countRows(payload.ledgers),
    vouchers: countRows(payload.vouchers),
    stock: countRows(payload.stock_items),
    outstanding: countRows(payload.outstanding),
    profit_loss: countRows(payload.profit_loss),
    balance_sheet: countRows(payload.balance_sheet),
    trial_balance: countRows(payload.trial_balance),
  };
}

function summarizeDomains(payload: SyncPayload) {
  return {
    masters: Boolean(
      payload.company_info
      || countRows(payload.groups)
      || countRows(payload.ledgers)
      || countRows(payload.stock_items),
    ),
    snapshots: Boolean(
      countRows(payload.outstanding)
      || countRows(payload.profit_loss)
      || countRows(payload.balance_sheet)
      || countRows(payload.trial_balance),
    ),
    vouchers: Boolean(countRows(payload.vouchers)),
  };
}

function normalizeContractVersion(value: unknown) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : 0;
}

function getSyncKey(request: Request) {
  return request.headers.get("x-sync-key")?.trim() || "";
}

function isDryRun(request: Request, payload: SyncPayload) {
  if (request.headers.get("x-sync-dry-run") === "1") {
    return true;
  }
  return Boolean(
    payload.sync_meta
    && typeof payload.sync_meta === "object"
    && (payload.sync_meta as Record<string, unknown>).dry_run === true,
  );
}

serve(async (request) => {
  if (request.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: corsHeaders });
  }

  if (request.method === "GET") {
    return jsonResponse({
      ok: true,
      service: "TallyBridge Direct Ingest",
      phase: INGEST_SKELETON_PHASE,
      contract_version: SYNC_CONTRACT_VERSION,
      direct_write_ready: false,
      supported_modes: ["dry-run"],
    });
  }

  if (request.method !== "POST") {
    return jsonResponse({ success: false, error: "Method not allowed" }, 405);
  }

  const expectedSyncKey = Deno.env.get("SYNC_INGEST_KEY")?.trim() || "";
  if (!expectedSyncKey) {
    return jsonResponse({
      success: false,
      error: "SYNC_INGEST_KEY is not configured for this function.",
    }, 500);
  }

  const providedSyncKey = getSyncKey(request);
  if (!providedSyncKey || providedSyncKey !== expectedSyncKey) {
    return jsonResponse({ success: false, error: "Unauthorized" }, 401);
  }

  let payload: SyncPayload;
  try {
    payload = await request.json();
  } catch {
    return jsonResponse({ success: false, error: "Request body must be valid JSON." }, 400);
  }

  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return jsonResponse({ success: false, error: "Sync payload must be a JSON object." }, 400);
  }

  const headerVersion = normalizeContractVersion(
    request.headers.get("x-sync-contract-version"),
  );
  const bodyVersion = normalizeContractVersion(payload.sync_contract_version);
  const effectiveVersion = headerVersion || bodyVersion;

  if (effectiveVersion != SYNC_CONTRACT_VERSION) {
    return jsonResponse({
      success: false,
      error: `Unsupported sync contract version ${effectiveVersion || "missing"}. Expected ${SYNC_CONTRACT_VERSION}.`,
      expected_contract_version: SYNC_CONTRACT_VERSION,
    }, 409);
  }

  if (typeof payload.company_name !== "string" || !payload.company_name.trim()) {
    return jsonResponse({ success: false, error: "company_name is required." }, 400);
  }

  if (!payload.sync_meta || typeof payload.sync_meta !== "object" || Array.isArray(payload.sync_meta)) {
    return jsonResponse({ success: false, error: "sync_meta must be an object." }, 400);
  }

  const domainHints = summarizeDomains(payload);
  const records = summarizeRecords(payload);

  if (!isDryRun(request, payload)) {
    return jsonResponse({
      success: false,
      error: "Phase 2 direct ingest skeleton is deployed, but database ingestors are not enabled yet. Use dry-run mode for endpoint validation only.",
      phase: INGEST_SKELETON_PHASE,
      contract_version: SYNC_CONTRACT_VERSION,
      domain_hints: domainHints,
      records,
    }, 501);
  }

  return jsonResponse({
    success: true,
    dry_run: true,
    phase: INGEST_SKELETON_PHASE,
    contract_version: SYNC_CONTRACT_VERSION,
    company_name: payload.company_name,
    domain_hints: domainHints,
    records,
    warnings: [
      "No database writes were performed. This is a Phase 2 dry-run validation response.",
    ],
  });
});

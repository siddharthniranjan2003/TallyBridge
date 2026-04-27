import { serve } from "https://deno.land/std@0.224.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.100.1";

import { INGEST_SKELETON_PHASE, SYNC_CONTRACT_VERSION } from "./contract.ts";

type SyncPayload = {
  company_name?: unknown;
  company_guid?: unknown;
  sync_contract_version?: unknown;
  sync_run_synced_at?: unknown;
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

const MAX_SECTION_ROWS = 100_000;
const MAX_VOUCHER_ROWS = 2_000;
const corsHeaders = {
  "access-control-allow-origin": "*",
  "access-control-allow-headers":
    "authorization, x-client-info, apikey, content-type, x-sync-key, x-sync-contract-version, x-sync-dry-run",
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
      payload.company_info ||
        countRows(payload.groups) ||
        countRows(payload.ledgers) ||
        countRows(payload.stock_items),
    ),
    snapshots: Boolean(
      countRows(payload.outstanding) ||
        countRows(payload.profit_loss) ||
        countRows(payload.balance_sheet) ||
        countRows(payload.trial_balance),
    ),
    vouchers: Boolean(payload.vouchers != null),
  };
}

function mergeRecordMaps(...maps: Array<Record<string, number>>) {
  const merged: Record<string, number> = {};
  for (const map of maps) {
    for (const [key, value] of Object.entries(map)) {
      if (Number.isFinite(value)) {
        merged[key] = value;
      }
    }
  }
  return merged;
}

function normalizeContractVersion(value: unknown) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : 0;
}

function normalizePositiveInteger(value: unknown) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}

function normalizeTrimmedString(value: unknown) {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function normalizeBoolean(value: unknown) {
  if (typeof value === "boolean") {
    return value;
  }
  if (typeof value === "string") {
    const normalized = value.trim().toLowerCase();
    if (normalized === "true" || normalized === "1") {
      return true;
    }
    if (normalized === "false" || normalized === "0") {
      return false;
    }
  }
  if (typeof value === "number") {
    if (value === 1) {
      return true;
    }
    if (value === 0) {
      return false;
    }
  }
  return null;
}

function normalizeSyncedAt(value: unknown) {
  if (typeof value !== "string" || !value.trim()) {
    return null;
  }

  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return null;
  }

  return parsed.toISOString();
}

function getSyncMetaRecord(payload: SyncPayload) {
  if (
    payload.sync_meta &&
    typeof payload.sync_meta === "object" &&
    !Array.isArray(payload.sync_meta)
  ) {
    return payload.sync_meta as Record<string, unknown>;
  }
  return {};
}

function normalizeRecordCounts(value: unknown) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return {};
  }

  const normalized: Record<string, number> = {};
  for (const [key, rawValue] of Object.entries(value)) {
    const parsed = Number(rawValue);
    if (Number.isFinite(parsed) && parsed >= 0) {
      normalized[key] = Math.trunc(parsed);
    }
  }
  return normalized;
}

function validateSectionArray(label: string, value: unknown) {
  if (value == null) {
    return null;
  }

  if (!Array.isArray(value)) {
    return `${label} must be an array when provided.`;
  }

  if (value.length > MAX_SECTION_ROWS) {
    return `${label} exceeds the maximum allowed size of ${MAX_SECTION_ROWS} rows.`;
  }

  return null;
}

function getSyncKey(request: Request) {
  return request.headers.get("x-sync-key")?.trim() || "";
}

function isDryRun(request: Request, payload: SyncPayload) {
  if (request.headers.get("x-sync-dry-run") === "1") {
    return true;
  }

  return Boolean(
    payload.sync_meta &&
      typeof payload.sync_meta === "object" &&
      (payload.sync_meta as Record<string, unknown>).dry_run === true,
  );
}

function timingSafeEqualStrings(provided: string, expected: string) {
  const encoder = new TextEncoder();
  const providedBytes = encoder.encode(provided);
  const expectedBytes = encoder.encode(expected);
  const maxLength = Math.max(providedBytes.length, expectedBytes.length);
  let diff = providedBytes.length ^ expectedBytes.length;

  for (let index = 0; index < maxLength; index += 1) {
    diff |= (providedBytes[index] ?? 0) ^ (expectedBytes[index] ?? 0);
  }

  return diff === 0;
}

function getSupabaseAdminConfig() {
  const url = Deno.env.get("SUPABASE_URL")?.trim() || "";
  const serviceRoleKey =
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")?.trim() ||
    Deno.env.get("SUPABASE_SERVICE_KEY")?.trim() ||
    "";

  return {
    url,
    serviceRoleKey,
    ready: Boolean(url && serviceRoleKey),
  };
}

function getSupabaseAdminClient() {
  const config = getSupabaseAdminConfig();
  if (!config.ready) {
    throw new Error(
      "Supabase admin client is not configured. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY for the Edge Function.",
    );
  }

  return createClient(config.url, config.serviceRoleKey, {
    auth: {
      autoRefreshToken: false,
      persistSession: false,
    },
  });
}

function buildCompanyUpsertPayload(
  companyName: string,
  companyGuid: string | null,
  companyInfo: unknown,
  alterIds: unknown,
) {
  const payload: Record<string, unknown> = { name: companyName };
  if (companyGuid) {
    payload.guid = companyGuid;
  }

  if (companyInfo && typeof companyInfo === "object") {
    const info = companyInfo as Record<string, unknown>;
    for (const key of [
      "books_from",
      "books_to",
      "books_from_raw",
      "books_to_raw",
      "gstin",
      "address",
      "guid",
      "state",
      "country",
      "pincode",
      "email",
      "phone",
      "gst_type",
      "pan",
    ]) {
      if (key in info && info[key] !== "" && info[key] != null) {
        if (key === "guid" && companyGuid) {
          continue;
        }
        payload[key] = info[key];
      }
    }

    if (typeof info.master_id === "number" && Number.isFinite(info.master_id)) {
      payload.master_id = info.master_id;
    }
  }

  if (alterIds && typeof alterIds === "object") {
    const ids = alterIds as Record<string, unknown>;
    for (const key of ["alter_id", "alt_vch_id", "alt_mst_id", "last_voucher_date"]) {
      if (key in ids && ids[key] !== "" && ids[key] != null) {
        payload[key] = ids[key];
      }
    }
  }

  return payload;
}

function withSchemaGuidance(message: string) {
  return `${message} Apply supabase/migrations/20260427_phase3_direct_ingest.sql and supabase/migrations/20260428_phase4_voucher_ingest.sql before using Phase 4 live writes.`;
}

async function upsertCompanyRecord(
  supabase: ReturnType<typeof getSupabaseAdminClient>,
  companyName: string,
  companyGuid: string | null,
  companyInfo: unknown,
  alterIds: unknown,
) {
  const payload = buildCompanyUpsertPayload(companyName, companyGuid, companyInfo, alterIds);

  if (companyGuid) {
    const { data: existingByGuid, error: guidLookupError } = await supabase
      .from("companies")
      .select("id")
      .eq("guid", companyGuid)
      .maybeSingle();

    if (guidLookupError) {
      throw new Error(`Company GUID lookup failed: ${guidLookupError.message}`);
    }

    if (existingByGuid?.id) {
      const { data: updatedCompany, error: updateError } = await supabase
        .from("companies")
        .update(payload)
        .eq("id", existingByGuid.id)
        .select("id")
        .single();

      if (updateError || !updatedCompany) {
        throw new Error(`Company update failed: ${updateError?.message}`);
      }

      return updatedCompany;
    }
  }

  const { data: nameMatches, error: nameLookupError } = await supabase
    .from("companies")
    .select("id, guid")
    .eq("name", companyName)
    .limit(2);

  if (nameLookupError) {
    throw new Error(`Company name lookup failed: ${nameLookupError.message}`);
  }

  if ((nameMatches || []).length > 1) {
    throw new Error(
      "Multiple companies already share this name. Re-add the company so sync can use the Tally GUID.",
    );
  }

  const matchedCompany = nameMatches?.[0];
  if (matchedCompany?.id && (!matchedCompany.guid || matchedCompany.guid === companyGuid)) {
    const { data: updatedCompany, error: updateError } = await supabase
      .from("companies")
      .update(payload)
      .eq("id", matchedCompany.id)
      .select("id")
      .single();

    if (updateError || !updatedCompany) {
      throw new Error(`Company update failed: ${updateError?.message}`);
    }

    return updatedCompany;
  }

  const { data: insertedCompany, error: insertError } = await supabase
    .from("companies")
    .insert(payload)
    .select("id")
    .single();

  if (insertError || !insertedCompany) {
    throw new Error(`Company insert failed: ${insertError?.message}`);
  }

  return insertedCompany;
}

serve(async (request) => {
  if (request.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: corsHeaders });
  }

  if (request.method === "GET") {
    const adminConfig = getSupabaseAdminConfig();
    const hasSyncKey = Boolean(Deno.env.get("SYNC_INGEST_KEY")?.trim());
    return jsonResponse({
      ok: true,
      service: "TallyBridge Direct Ingest",
      phase: INGEST_SKELETON_PHASE,
      contract_version: SYNC_CONTRACT_VERSION,
      direct_write_ready: adminConfig.ready && hasSyncKey,
      supported_modes: ["dry-run", "live-write"],
      supported_domains: ["masters", "snapshots", "vouchers"],
      voucher_live_write_supported: true,
    });
  }

  if (request.method !== "POST") {
    return jsonResponse({ success: false, error: "Method not allowed" }, 405);
  }

  const expectedSyncKey = Deno.env.get("SYNC_INGEST_KEY")?.trim() || "";
  if (!expectedSyncKey) {
    return jsonResponse(
      {
        success: false,
        error: "SYNC_INGEST_KEY is not configured for this function.",
      },
      500,
    );
  }

  const providedSyncKey = getSyncKey(request);
  if (
    !providedSyncKey ||
    !timingSafeEqualStrings(providedSyncKey, expectedSyncKey)
  ) {
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

  if (effectiveVersion !== SYNC_CONTRACT_VERSION) {
    return jsonResponse(
      {
        success: false,
        error: `Unsupported sync contract version ${effectiveVersion || "missing"}. Expected ${SYNC_CONTRACT_VERSION}.`,
        expected_contract_version: SYNC_CONTRACT_VERSION,
      },
      409,
    );
  }

  if (typeof payload.company_name !== "string" || !payload.company_name.trim()) {
    return jsonResponse({ success: false, error: "company_name is required." }, 400);
  }

  if (
    !payload.sync_meta ||
    typeof payload.sync_meta !== "object" ||
    Array.isArray(payload.sync_meta)
  ) {
    return jsonResponse({ success: false, error: "sync_meta must be an object." }, 400);
  }

  const validationError = [
    validateSectionArray("groups", payload.groups),
    validateSectionArray("ledgers", payload.ledgers),
    validateSectionArray("vouchers", payload.vouchers),
    validateSectionArray("stock_items", payload.stock_items),
    validateSectionArray("outstanding", payload.outstanding),
    validateSectionArray("profit_loss", payload.profit_loss),
    validateSectionArray("balance_sheet", payload.balance_sheet),
    validateSectionArray("trial_balance", payload.trial_balance),
  ].find(Boolean);

  if (validationError) {
    return jsonResponse({ success: false, error: validationError }, 400);
  }

  if (countRows(payload.vouchers) > MAX_VOUCHER_ROWS) {
    return jsonResponse(
      {
        success: false,
        error: `vouchers exceeds the per-request maximum of ${MAX_VOUCHER_ROWS} rows. Chunk voucher uploads from the desktop client.`,
        max_voucher_rows: MAX_VOUCHER_ROWS,
      },
      400,
    );
  }

  const domainHints = summarizeDomains(payload);
  const records = summarizeRecords(payload);
  const dryRun = isDryRun(request, payload);
  const syncMeta = getSyncMetaRecord(payload);
  const chunkIndex =
    normalizePositiveInteger(syncMeta.chunk_index) ||
    normalizePositiveInteger(syncMeta.voucher_chunk_index) ||
    (domainHints.vouchers ? 1 : null);
  const chunkCount =
    normalizePositiveInteger(syncMeta.chunk_count) ||
    normalizePositiveInteger(syncMeta.voucher_chunk_count) ||
    (chunkIndex ? Math.max(chunkIndex, 1) : null);
  const isFinalChunk =
    normalizeBoolean(syncMeta.is_final_chunk) ??
    (chunkIndex != null && chunkCount != null ? chunkIndex >= chunkCount : false);
  const recordCounts = normalizeRecordCounts(syncMeta.record_counts);

  if (dryRun) {
    return jsonResponse({
      success: true,
      dry_run: true,
      phase: INGEST_SKELETON_PHASE,
      contract_version: SYNC_CONTRACT_VERSION,
      company_name: payload.company_name,
      domain_hints: domainHints,
      records,
      chunk: domainHints.vouchers
        ? {
            chunk_index: chunkIndex,
            chunk_count: chunkCount,
            is_final_chunk: isFinalChunk,
            max_voucher_rows: MAX_VOUCHER_ROWS,
          }
        : null,
      warnings: [
        "No database writes were performed. This is a dry-run validation response.",
      ],
    });
  }

  try {
    const supabase = getSupabaseAdminClient();
    const normalizedCompanyName = payload.company_name.trim();
    const normalizedCompanyGuid =
      normalizeTrimmedString(payload.company_guid) ||
      (payload.company_info &&
      typeof payload.company_info === "object"
        ? normalizeTrimmedString(
            (payload.company_info as Record<string, unknown>).guid,
          )
        : null);
    const syncedAt =
      normalizeSyncedAt(payload.sync_run_synced_at) ||
      new Date().toISOString();

    const company = await upsertCompanyRecord(
      supabase,
      normalizedCompanyName,
      normalizedCompanyGuid,
      payload.company_info,
      payload.alter_ids,
    );
    const companyId = company.id as string;

    let directRecords: Record<string, number> = {};
    if (domainHints.masters || domainHints.snapshots) {
      const { data, error } = await supabase.rpc("tb_ingest_phase3_hybrid", {
        p_company_id: companyId,
        p_synced_at: syncedAt,
        p_groups: payload.groups ?? null,
        p_ledgers: payload.ledgers ?? null,
        p_stock_items: payload.stock_items ?? null,
        p_outstanding: payload.outstanding ?? null,
        p_profit_loss: payload.profit_loss ?? null,
        p_balance_sheet: payload.balance_sheet ?? null,
        p_trial_balance: payload.trial_balance ?? null,
      });

      if (error) {
        throw new Error(withSchemaGuidance(error.message));
      }

      if (data && typeof data === "object" && !Array.isArray(data)) {
        directRecords = data as Record<string, number>;
      }
    }

    let voucherRecords: Record<string, number> = {};
    if (payload.vouchers != null) {
      const { data, error } = await supabase.rpc("tb_ingest_vouchers", {
        p_company_id: companyId,
        p_synced_at: syncedAt,
        p_vouchers: payload.vouchers ?? [],
        p_sync_meta: payload.sync_meta ?? {},
        p_alter_ids: payload.alter_ids ?? {},
        p_is_final_chunk: isFinalChunk,
        p_record_counts: recordCounts,
      });

      if (error) {
        throw new Error(withSchemaGuidance(error.message));
      }

      if (data && typeof data === "object" && !Array.isArray(data)) {
        voucherRecords = data as Record<string, number>;
      }
    }

    const combinedRecords = mergeRecordMaps(directRecords, voucherRecords);

    return jsonResponse({
      success: true,
      dry_run: false,
      phase: INGEST_SKELETON_PHASE,
      contract_version: SYNC_CONTRACT_VERSION,
      company_name: normalizedCompanyName,
      company_id: companyId,
      domain_hints: domainHints,
      records: combinedRecords,
      synced_at: syncedAt,
      direct_write_ready: true,
      chunk: domainHints.vouchers
        ? {
            chunk_index: chunkIndex,
            chunk_count: chunkCount,
            is_final_chunk: isFinalChunk,
            max_voucher_rows: MAX_VOUCHER_ROWS,
          }
        : null,
      warnings:
        domainHints.masters || domainHints.snapshots || payload.vouchers != null
          ? []
          : ["No direct-ingest sections were provided, so only company identity was refreshed."],
    });
  } catch (error) {
    const message =
      error instanceof Error
        ? error.message
        : "Phase 4 direct ingest failed unexpectedly.";
    return jsonResponse({ success: false, error: message }, 500);
  }
});

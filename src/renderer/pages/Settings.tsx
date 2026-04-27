import { useEffect, useMemo, useState } from "react";

type CompanyRef = {
  name: string;
  tallyGuid?: string;
};

type TallyCompanyDateRange = {
  name: string;
  guid?: string;
  booksFrom: string | null;
  booksTo: string | null;
  availableFromDates: string[];
};

type SettingsForm = {
  tallyUrl: string;
  syncIntervalMinutes: number;
  controlPlaneUrl: string;
  controlPlaneApiKey: string;
  syncIngestMode: "render" | "direct";
  syncIngestUrl: string;
  syncIngestKey: string;
  syncContractVersion: number;
  accountEmail: string;
  readMode: "auto" | "xml-only" | "hybrid";
  odbcDsnOverride: string;
  syncFromDate: string;
  syncToDate: string;
};

const DEFAULT_FORM: SettingsForm = {
  tallyUrl: "http://localhost:9000",
  syncIntervalMinutes: 5,
  controlPlaneUrl: "",
  controlPlaneApiKey: "",
  syncIngestMode: "render",
  syncIngestUrl: "",
  syncIngestKey: "",
  syncContractVersion: 1,
  accountEmail: "",
  readMode: "auto",
  odbcDsnOverride: "",
  syncFromDate: "",
  syncToDate: "",
};

function getRangeKey(range: Pick<TallyCompanyDateRange, "name" | "guid">) {
  return range.guid?.trim() || range.name.trim().toLowerCase();
}

function clampIsoDate(value: string, min?: string | null, max?: string | null) {
  if (!value) {
    return value;
  }

  let next = value;
  if (min && next < min) {
    next = min;
  }
  if (max && next > max) {
    next = max;
  }
  return next;
}

function pickPreferredRangeKey(
  ranges: TallyCompanyDateRange[],
  configuredCompanies: CompanyRef[],
) {
  const preferredKeys = configuredCompanies
    .map((company) => company.tallyGuid?.trim() || company.name.trim().toLowerCase())
    .filter(Boolean);

  for (const key of preferredKeys) {
    const match = ranges.find((range) => getRangeKey(range) === key);
    if (match) {
      return getRangeKey(match);
    }
  }

  return "";
}

export default function Settings() {
  const [form, setForm] = useState<SettingsForm>(DEFAULT_FORM);
  const [saved, setSaved] = useState(false);
  const [tallyOk, setTallyOk] = useState<boolean | null>(null);
  const [capabilities, setCapabilities] = useState<any | null>(null);
  const [configuredCompanies, setConfiguredCompanies] = useState<CompanyRef[]>([]);
  const [companyRanges, setCompanyRanges] = useState<TallyCompanyDateRange[]>([]);
  const [selectedRangeKey, setSelectedRangeKey] = useState("");
  const [loadingRanges, setLoadingRanges] = useState(false);
  const [rangeError, setRangeError] = useState<string | null>(null);

  const selectedCompanyRange = useMemo(
    () => companyRanges.find((range) => getRangeKey(range) === selectedRangeKey) || null,
    [companyRanges, selectedRangeKey],
  );
  const syncFromOptions = selectedCompanyRange?.availableFromDates || [];
  const syncToMin = form.syncFromDate || selectedCompanyRange?.booksFrom || "";
  const syncToMax = selectedCompanyRange?.booksTo || "";

  const set = <K extends keyof SettingsForm>(key: K, val: SettingsForm[K]) =>
    setForm((current) => ({ ...current, [key]: val }));

  const loadCompanyDateRanges = async (preferredCompanies: CompanyRef[] = []) => {
    setLoadingRanges(true);
    setRangeError(null);
    const response = await window.electronAPI.getTallyCompanyDateRanges();
    setLoadingRanges(false);

    if (!response.success) {
      setCompanyRanges([]);
      setSelectedRangeKey("");
      setRangeError(response.error || "Could not read company date ranges from ERP 9.");
      return;
    }

    if (!response.companies?.length) {
      setCompanyRanges([]);
      setSelectedRangeKey("");
      setRangeError("No company range was returned by ERP 9.");
      return;
    }

    setCompanyRanges(response.companies);
    const preferredKey = pickPreferredRangeKey(response.companies, preferredCompanies);
    const nextKey = response.companies.some((range) => getRangeKey(range) === selectedRangeKey)
      ? selectedRangeKey
      : (preferredKey || getRangeKey(response.companies[0]));
    setSelectedRangeKey(nextKey);

    const selected = response.companies.find((range) => getRangeKey(range) === nextKey);
    if (!selected) {
      return;
    }

    setForm((current) => {
      const nextFrom = selected.availableFromDates.includes(current.syncFromDate)
        ? current.syncFromDate
        : (selected.availableFromDates[0] || selected.booksFrom || current.syncFromDate || "");
      const nextTo = clampIsoDate(
        current.syncToDate || selected.booksTo || "",
        nextFrom || selected.booksFrom,
        selected.booksTo,
      );
      return {
        ...current,
        syncFromDate: nextFrom,
        syncToDate: nextTo,
      };
    });
  };

  useEffect(() => {
    let active = true;
    (async () => {
      const cfg = await window.electronAPI.getConfig();
      if (!active) {
        return;
      }

      const companies = (cfg.companies || []) as CompanyRef[];
      setConfiguredCompanies(companies);
      setForm({
        tallyUrl: cfg.tallyUrl || DEFAULT_FORM.tallyUrl,
        syncIntervalMinutes: cfg.syncIntervalMinutes || DEFAULT_FORM.syncIntervalMinutes,
        controlPlaneUrl: cfg.controlPlaneUrl || cfg.backendUrl || "",
        controlPlaneApiKey: cfg.controlPlaneApiKey || cfg.apiKey || "",
        syncIngestMode: cfg.syncIngestMode === "direct" ? "direct" : "render",
        syncIngestUrl: cfg.syncIngestUrl || "",
        syncIngestKey: cfg.syncIngestKey || "",
        syncContractVersion: cfg.syncContractVersion || DEFAULT_FORM.syncContractVersion,
        accountEmail: cfg.accountEmail || "",
        readMode: cfg.readMode || DEFAULT_FORM.readMode,
        odbcDsnOverride: cfg.odbcDsnOverride || "",
        syncFromDate: cfg.syncFromDate || "",
        syncToDate: cfg.syncToDate || "",
      });

      await loadCompanyDateRanges(companies);
    })();

    return () => {
      active = false;
    };
  }, []);

  const handleSave = async () => {
    await window.electronAPI.saveSettings(form);
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  };

  const testTally = async () => {
    setTallyOk(null);
    const result = await window.electronAPI.checkTally();
    setTallyOk(result.connected);
  };

  const checkCapabilities = async () => {
    const result = await window.electronAPI.checkTallyCapabilities();
    setCapabilities(result);
  };

  const onSelectRangeCompany = (nextKey: string) => {
    setSelectedRangeKey(nextKey);
    const selected = companyRanges.find((range) => getRangeKey(range) === nextKey);
    if (!selected) {
      return;
    }

    setForm((current) => {
      const nextFrom = selected.availableFromDates.includes(current.syncFromDate)
        ? current.syncFromDate
        : (selected.availableFromDates[0] || selected.booksFrom || "");
      const nextTo = clampIsoDate(
        current.syncToDate || selected.booksTo || "",
        nextFrom || selected.booksFrom,
        selected.booksTo,
      );
      return {
        ...current,
        syncFromDate: nextFrom,
        syncToDate: nextTo,
      };
    });
  };

  const onSyncFromDateChange = (value: string) => {
    setForm((current) => ({
      ...current,
      syncFromDate: value,
      syncToDate: clampIsoDate(
        current.syncToDate,
        value || selectedCompanyRange?.booksFrom,
        selectedCompanyRange?.booksTo,
      ),
    }));
  };

  const useCompanyFullRange = () => {
    if (!selectedCompanyRange) {
      return;
    }

    const nextFrom = selectedCompanyRange.availableFromDates[0] || selectedCompanyRange.booksFrom || "";
    const nextTo = clampIsoDate(
      selectedCompanyRange.booksTo || "",
      nextFrom || selectedCompanyRange.booksFrom,
      selectedCompanyRange.booksTo,
    );

    setForm((current) => ({
      ...current,
      syncFromDate: nextFrom,
      syncToDate: nextTo,
    }));
  };

  return (
    <div style={{ padding: 28, maxWidth: 560 }}>
      <h1 style={{ fontSize: 20, fontWeight: 600, marginBottom: 28 }}>Settings</h1>

      <Section title="TallyPrime Connection">
        <Field label="Tally URL" hint="Default: http://localhost:9000">
          <div style={{ display: "flex", gap: 8 }}>
            <input value={form.tallyUrl} onChange={(e) => set("tallyUrl", e.target.value)} />
            <button onClick={testTally} style={testBtn}>Test</button>
          </div>
          {tallyOk === true && (
            <p style={{ color: "#22c55e", fontSize: 12, marginTop: 4 }}>Connected to TallyPrime</p>
          )}
          {tallyOk === false && (
            <p style={{ color: "#ef4444", fontSize: 12, marginTop: 4 }}>
              Cannot connect. Is TallyPrime open?
            </p>
          )}
        </Field>

        <Field label="Sync Interval" hint="How often to sync (1-60 minutes)">
          <select
            value={form.syncIntervalMinutes}
            onChange={(e) => set("syncIntervalMinutes", Number(e.target.value))}
            style={{ width: "100%", padding: "9px 12px", borderRadius: 8, border: "1px solid #dee2e6", background: "#fff" }}
          >
            {[1, 2, 5, 10, 15, 30, 60].map((minutes) => (
              <option key={minutes} value={minutes}>
                {minutes} {minutes === 1 ? "minute" : "minutes"}
              </option>
            ))}
          </select>
        </Field>

        <Field label="Read Mode" hint="Auto prefers ODBC for supported ERP 9 master reads and falls back to XML">
          <select
            value={form.readMode}
            onChange={(e) => set("readMode", e.target.value as SettingsForm["readMode"])}
            style={{ width: "100%", padding: "9px 12px", borderRadius: 8, border: "1px solid #dee2e6", background: "#fff" }}
          >
            <option value="auto">Auto</option>
            <option value="xml-only">XML only</option>
            <option value="hybrid">Hybrid</option>
          </select>
        </Field>

        <Field label="ODBC DSN Override" hint="Optional. Leave blank unless you know the exact Tally ODBC DSN name.">
          <input
            value={form.odbcDsnOverride}
            onChange={(e) => set("odbcDsnOverride", e.target.value)}
            placeholder="TallyODBC_9000"
          />
        </Field>

        <Field label="ERP 9 Company Date Range" hint="Fetches books range from the ERP 9 company.">
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <select
              value={selectedRangeKey}
              onChange={(e) => onSelectRangeCompany(e.target.value)}
              style={{ width: "100%", minWidth: 220, flex: 1, padding: "9px 12px", borderRadius: 8, border: "1px solid #dee2e6", background: "#fff" }}
              disabled={!companyRanges.length}
            >
              {!companyRanges.length && <option value="">No company range loaded</option>}
              {companyRanges.map((range) => (
                <option key={getRangeKey(range)} value={getRangeKey(range)}>
                  {range.name}
                </option>
              ))}
            </select>
            <button onClick={() => loadCompanyDateRanges(configuredCompanies)} style={testBtn} disabled={loadingRanges}>
              {loadingRanges ? "Loading..." : "Refresh Ranges"}
            </button>
            <button onClick={useCompanyFullRange} style={testBtn} disabled={!selectedCompanyRange}>
              Use Full Range
            </button>
          </div>
          {selectedCompanyRange && (
            <p style={{ fontSize: 12, color: "#6b7280", marginTop: 6 }}>
              Available in ERP 9: {selectedCompanyRange.booksFrom || "unknown"} to {selectedCompanyRange.booksTo || "today"}
            </p>
          )}
          {rangeError && (
            <p style={{ fontSize: 12, color: "#ef4444", marginTop: 6 }}>{rangeError}</p>
          )}
        </Field>

        <Field
          label="Sync From Date"
          hint={syncFromOptions.length ? "Choose a date available in ERP 9 company data." : "Optional. Overrides auto FY start for backfill."}
        >
          {syncFromOptions.length ? (
            <select
              value={form.syncFromDate}
              onChange={(e) => onSyncFromDateChange(e.target.value)}
              style={{ width: "100%", padding: "9px 12px", borderRadius: 8, border: "1px solid #dee2e6", background: "#fff" }}
            >
              {syncFromOptions.map((dateValue) => (
                <option key={dateValue} value={dateValue}>{dateValue}</option>
              ))}
            </select>
          ) : (
            <input
              type="date"
              value={form.syncFromDate}
              onChange={(e) => onSyncFromDateChange(e.target.value)}
            />
          )}
        </Field>

        <Field label="Sync To Date" hint="Bounded to selected company date range when available.">
          <input
            type="date"
            value={form.syncToDate}
            min={syncToMin || undefined}
            max={syncToMax || undefined}
            onChange={(e) => set("syncToDate", e.target.value)}
          />
        </Field>

        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <button onClick={checkCapabilities} style={testBtn}>Check Capabilities</button>
          {capabilities && (
            <span style={{ fontSize: 12, color: "#6b7280" }}>
              XML: {capabilities.xml?.connected ? "OK" : "Down"} | ODBC: {capabilities.odbc?.state || "unknown"}
            </span>
          )}
        </div>
        {capabilities && (
          <div
            style={{
              border: "1px solid #e5e7eb",
              borderRadius: 8,
              padding: 12,
              background: "#fff",
              fontSize: 12,
              color: "#374151",
            }}
          >
            <div>
              Transport plan: vouchers and reports use XML; masters use {capabilities.odbc?.state === "ok" ? "ODBC first" : "XML"}.
            </div>
            {capabilities.odbc?.dsn && <div>ODBC DSN: {capabilities.odbc.dsn}</div>}
            {capabilities.odbc?.message && <div>ODBC: {capabilities.odbc.message}</div>}
            {capabilities.xml?.error && <div>XML: {capabilities.xml.error}</div>}
          </div>
        )}
      </Section>

      <Section title="Control Plane">
        <Field label="Control Plane URL" hint="Render stays the control plane for queueing, alter-id checks, and read APIs.">
          <input
            value={form.controlPlaneUrl}
            onChange={(e) => set("controlPlaneUrl", e.target.value)}
            placeholder="https://your-app.onrender.com"
          />
        </Field>
        <Field label="Control Plane API Key" hint="Used for Render control-plane endpoints.">
          <input
            type="password"
            value={form.controlPlaneApiKey}
            onChange={(e) => set("controlPlaneApiKey", e.target.value)}
            placeholder="Enter your Render API key"
          />
        </Field>
        <Field label="Account Email">
          <input
            type="email"
            value={form.accountEmail}
            onChange={(e) => set("accountEmail", e.target.value)}
            placeholder="you@example.com"
          />
        </Field>
      </Section>

      <Section title="Direct Ingest">
        <Field label="Sync Ingest Mode" hint="Keep Render mode on until the direct ingest endpoint is deployed and tested.">
          <select
            value={form.syncIngestMode}
            onChange={(e) => set("syncIngestMode", e.target.value as SettingsForm["syncIngestMode"])}
            style={{ width: "100%", padding: "9px 12px", borderRadius: 8, border: "1px solid #dee2e6", background: "#fff" }}
          >
            <option value="render">Render /api/sync (default)</option>
            <option value="direct">Direct ingest (Phase 2)</option>
          </select>
        </Field>

        <Field label="Sync Ingest URL" hint="Supabase Edge Function URL for the direct ingest path.">
          <input
            value={form.syncIngestUrl}
            onChange={(e) => set("syncIngestUrl", e.target.value)}
            placeholder="https://your-project.supabase.co/functions/v1/ingest-sync"
          />
        </Field>

        <Field label="Sync Ingest Key" hint="Used only by the direct ingest endpoint.">
          <input
            type="password"
            value={form.syncIngestKey}
            onChange={(e) => set("syncIngestKey", e.target.value)}
            placeholder="Enter your direct ingest key"
          />
        </Field>

        <Field label="Sync Contract Version" hint="Desktop and ingest endpoint must agree on this contract version.">
          <input
            type="number"
            min={1}
            step={1}
            value={form.syncContractVersion}
            onChange={(e) => set("syncContractVersion", Number(e.target.value) || 1)}
          />
        </Field>
      </Section>

      <button onClick={handleSave} style={{ ...primaryBtn, width: "100%" }}>
        {saved ? "Settings Saved!" : "Save Settings"}
      </button>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 28 }}>
      <p style={{ fontSize: 11, fontWeight: 600, color: "#adb5bd", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 14 }}>
        {title}
      </p>
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {children}
      </div>
    </div>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div>
      <label style={{ display: "block", fontSize: 13, fontWeight: 500, marginBottom: 5, color: "#374151" }}>
        {label}
      </label>
      {children}
      {hint && <p style={{ fontSize: 11, color: "#adb5bd", marginTop: 4 }}>{hint}</p>}
    </div>
  );
}

const primaryBtn: React.CSSProperties = {
  background: "#1a1a2e",
  color: "#fff",
  border: "none",
  borderRadius: 8,
  padding: "11px 20px",
  cursor: "pointer",
  fontSize: 14,
  fontWeight: 500,
};

const testBtn: React.CSSProperties = {
  background: "transparent",
  border: "1px solid #dee2e6",
  borderRadius: 8,
  padding: "9px 14px",
  cursor: "pointer",
  fontSize: 13,
  whiteSpace: "nowrap",
  flexShrink: 0,
};

export const meta = {
  name: 'tallybridge-arch-and-prod-audit',
  description: 'Map architecture of TallyBridge (Electron+Python+2 backends) and aiaccountant Flutter, then assess production-readiness across 6 dimensions',
  phases: [
    { title: 'Architecture', detail: '6 parallel readers map each subsystem' },
    { title: 'Production-Readiness', detail: '6 parallel assessors grade testing/security/cicd/observability/data/quality' },
  ],
}

const ARCH_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['component', 'purpose', 'entrypoints', 'keyModules', 'externalInterfaces', 'dataFlows', 'configAndSecrets', 'risks', 'techDebt'],
  properties: {
    component: { type: 'string' },
    purpose: { type: 'string', description: '2-3 sentence summary of what this subsystem does' },
    entrypoints: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['file', 'role'], properties: { file: { type: 'string' }, role: { type: 'string' } } } },
    keyModules: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['file', 'responsibility', 'loc'], properties: { file: { type: 'string' }, responsibility: { type: 'string' }, loc: { type: 'number' } } } },
    externalInterfaces: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['target', 'protocol', 'notes'], properties: { target: { type: 'string' }, protocol: { type: 'string' }, notes: { type: 'string' } } }, description: 'ports, APIs, DBs, cloud services this talks to' },
    dataFlows: { type: 'array', items: { type: 'string' }, description: 'concrete end-to-end flows through this component' },
    configAndSecrets: { type: 'array', items: { type: 'string' }, description: 'env vars, stored config keys, secrets and how they are loaded' },
    risks: { type: 'array', items: { type: 'string' }, description: 'correctness/reliability risks specific to this component' },
    techDebt: { type: 'array', items: { type: 'string' } },
  },
}

const PROD_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['dimension', 'currentState', 'findings', 'recommendations', 'toolingChoices', 'effort'],
  properties: {
    dimension: { type: 'string' },
    currentState: { type: 'string', description: 'honest assessment of where this dimension stands today' },
    findings: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['title', 'severity', 'evidence', 'impact', 'recommendation'], properties: {
      title: { type: 'string' },
      severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
      evidence: { type: 'string', description: 'file:line or concrete observation' },
      impact: { type: 'string' },
      recommendation: { type: 'string' },
    } } },
    recommendations: { type: 'array', items: { type: 'string' }, description: 'prioritized concrete actions' },
    toolingChoices: { type: 'array', items: { type: 'string' }, description: 'specific frameworks/tools to adopt with rationale' },
    effort: { type: 'string', description: 'rough sizing (e.g. S/M/L per workstream)' },
  },
}

const TB = 'D:/Desktop/TallyBridge'
const AIA = 'D:/Desktop/Ai_Accountant/aiaccountant'

phase('Architecture')

const archTasks = [
  {
    label: 'arch:electron-main',
    prompt: `You are mapping the architecture of the ELECTRON MAIN PROCESS of TallyBridge, a Windows desktop app that syncs TallyPrime accounting software to Supabase cloud.
Read these files fully under ${TB}/src/main/ : index.ts, sync-engine.ts, local-push-server.ts, push-queue-poller.ts, ipc-handlers.ts, store.ts, updater.ts, remote-log.ts, tray.ts, logger.ts, preload.ts.
Focus on: process orchestration, how the Python engine subprocess is spawned/tracked/killed, the local HTTP server on port 3002, the cloud push-queue poller, IPC surface to the renderer, electron-store config keys and one-time migrations, auto-update flow, and lifecycle (startup/shutdown/tray). Note any reliability landmines (unhandled spawns, port races, single-threaded Tally hits, swallowed errors). Return the architecture per the schema. Use real file:line evidence.`,
  },
  {
    label: 'arch:renderer',
    prompt: `You are mapping the architecture of the REACT RENDERER (Vite+React19+Tailwind) of TallyBridge desktop app.
Read these under ${TB}/src/renderer/ : App.tsx, main.tsx, electron.d.ts, pages/*.tsx (Home, HomeGuided, Settings, AddCompany, AddCompanyGuided, SyncLog, About), components/*.tsx (Sidebar, StatusBar, CompanyCard*, UpdateBanner).
Focus on: routing, how the UI talks to main via IPC/preload, settings surface (which config it writes), the "Guided" vs non-guided duplication, state management, and any UX/production gaps (error states, loading, accessibility). Return architecture per schema with file:line evidence.`,
  },
  {
    label: 'arch:python-engine',
    prompt: `You are mapping the PYTHON SYNC ENGINE of TallyBridge (spawned as subprocess by Electron).
Read fully under ${TB}/src/python/ : sync_main.py (1905 loc), tally_client.py, cloud_pusher.py (933 loc), tally_pusher.py, odbc_bridge.py, xml_parser.py, definition_extractor.py, proxy.py, diagnose.py.
Focus on: entry/decision logic in sync_main (read modes auto/xml-only/hybrid/shadow, ingest modes render/hybrid/direct), how it reads TallyPrime XML-RPC on port 9000 with ODBC fallback, how cloud_pusher batches/POSTs to backend AND writes direct to Supabase, the push path (tally_pusher writes vouchers back to Tally), error handling, retries, and the known single-threaded Tally-9000 hang risk. Return architecture per schema with file:line evidence.`,
  },
  {
    label: 'arch:backend-express',
    prompt: `You are mapping the EXPRESS CLOUD BACKEND of TallyBridge (port 3001, deployed on GCP Cloud Run).
Read fully under ${TB}/backend/src/ : index.ts, middleware/auth.ts, routes/sync.ts (2992 loc — read it carefully in chunks), routes/push-voucher.ts, routes/push-invoice.ts, db/supabase.ts, db/firebase.ts, db/gcs.ts. Also skim the SQL files at ${TB}/backend/*.sql and ${TB}/backend/src/**/*.sql to understand the schema.
Focus on: auth (Firebase JWT + API key via timingSafeEqual), the giant sync.ts ingest endpoint and every section type it handles, idempotency/upsert strategy, the RPC tb_ingest_masters path, multi-tenant routing (one Supabase project per client), body size limits, and any injection/validation gaps. Return architecture per schema with file:line evidence. List the SQL schema files you found.`,
  },
  {
    label: 'arch:parsing-ocr',
    prompt: `You are mapping the PARSING / OCR BACKEND of TallyBridge (Flask, deployed on GCP Cloud Run + RunPod serverless GPU workers) that turns scanned/PDF invoices into Tally vouchers.
Read under ${TB}/parsing/ : the Flask servers (nanonets_invoice_server.py, paddleocr_vl_server.py, docstrange_invoice_server.py, n8n_minicpm_server.py), the pipelines (purchase_paddle_runner.py, purchase_image_pipeline.py, purchase_ocrvl_pipeline.py, purchase_docstrange_pipeline.py), the purchase/ package (vendor.py, voucher_builder.py, vlm_vendor_parser.py, company_context.py, models.py), lib/ (env.py, text.py, numeric.py), server/handler.py, and the serverless workers (minicpm-serverless-worker/handler.py, runpod-serverless-worker/handler.py). Read requirements.txt and Dockerfile.
Focus on: which server/pipeline is the CURRENT production path vs experimental, the OCR→VLM→voucher flow, how it reads/writes Supabase, RunPod endpoint coupling, secret handling, and how it connects to the sale/purchase push. Identify what is dead/experimental. Return architecture per schema with file:line evidence.`,
  },
  {
    label: 'arch:aiaccountant-flutter',
    prompt: `You are mapping the AIACCOUNTANT FLUTTER FRONTEND (mobile/web client) at ${AIA}.
Read pubspec.yaml, lib/main.dart, lib/firebase_options.dart, and explore lib/core, lib/data, lib/services, lib/shared, and each feature under lib/features/ (auth, camera, history, profile, queue, report, shell). Read the most important file in each feature dir. Also read README.md, .firebaserc, firebase.json, and note env/ dart-define multi-flavor setup (testing/deployment/prod).
Focus on: app architecture (state mgmt, navigation, data layer), how it auths via Firebase and talks to the SAME Supabase project (ztugw client prod) directly + the backend, the camera→invoice-scan→parsing flow, the multi-env flavor system, and production gaps (no tests, hardcoded config, error handling). Return architecture per schema with file:line evidence.`,
  },
]

const archResults = (await parallel(archTasks.map(t => () => agent(t.prompt, { label: t.label, phase: 'Architecture', schema: ARCH_SCHEMA })))).filter(Boolean)

// Build a compact digest to feed phase 2
const digest = archResults.map(r => {
  const ifaces = (r.externalInterfaces || []).map(i => `${i.target}(${i.protocol})`).join(', ')
  const risks = (r.risks || []).slice(0, 4).join(' | ')
  const debt = (r.techDebt || []).slice(0, 4).join(' | ')
  return `### ${r.component}\nPurpose: ${r.purpose}\nInterfaces: ${ifaces}\nRisks: ${risks}\nTechDebt: ${debt}`
}).join('\n\n')

log(`Architecture mapped for ${archResults.length} subsystems. Digest ${digest.length} chars. Starting production-readiness assessment.`)

phase('Production-Readiness')

const DIGEST_HEADER = `You are assessing the TallyBridge system for PRODUCTION readiness (it will be shipped to a paying client). Here is the architecture digest from the mapping phase:\n\n${digest}\n\nThe system: a Windows Electron desktop connector (src/main TS + src/python Python engine) that reads TallyPrime via XML-RPC port 9000, an Express backend on GCP Cloud Run (backend/, 2992-line sync.ts), a Flask+RunPod OCR backend (parsing/), one Supabase project per client (yynuu=testing, ztugw=client prod), and a Flutter app (aiaccountant) for mobile/web. There is currently NO CI/CD (no .github/workflows) and NO automated tests (backend test script is 'exit 1'). Repos: ${TB} and ${AIA}.\n\n`

const prodTasks = [
  {
    label: 'prod:testing',
    prompt: DIGEST_HEADER + `DIMENSION = AUTOMATED TESTING STRATEGY. This is the user's top priority — they explicitly want a recommendation on automated testing.
Read package.json test scripts in ${TB}, ${TB}/backend, and pubspec/test dir in ${AIA}. Confirm what tests exist (essentially none). Then design a concrete, pragmatic testing pyramid for THIS system:
- Unit tests: backend route handlers + parsing helpers (lib/numeric, text) + python xml_parser + electron store/migrations. Recommend frameworks (Vitest/Jest for TS, pytest for Python, flutter_test for Dart).
- Integration tests: backend sync.ts ingest against a Supabase test project or local postgres; the Tally XML parsing against recorded XML fixtures (so you DON'T need a live Tally for CI).
- Contract tests: the Electron<->backend<->Supabase boundaries.
- E2E: how to test the Electron app (Playwright for Electron) and the desktop sync loop with a MOCKED Tally (record/replay port 9000 XML so CI never needs TallyPrime). The single-threaded Tally-9000 hang means live testing is dangerous — emphasize fixture/replay.
- What to test FIRST (highest ROI given accounting data correctness matters): idempotency of ingest, GST/discount/net-amount math, voucher-build correctness, push-queue dedup.
Give a phased rollout and target coverage. Be specific with file:line evidence of what's currently untested and fragile. Return per schema.`,
  },
  {
    label: 'prod:security',
    prompt: DIGEST_HEADER + `DIMENSION = SECURITY & SECRETS MANAGEMENT.
Investigate: committed secrets (check whether ${TB}/backend/.env, ${TB}/parsing/.env, ${AIA}/.env, backend.env.yaml are tracked by git and contain live keys — run git ls-files and read them; report what TYPES of secrets are exposed without printing full secret values), the auth model (backend/src/middleware/auth.ts timingSafeEqual API key + Firebase JWT), Supabase service_role key usage vs RLS (memory notes parsing must use service_role; RLS gaps mean direct-from-Flutter writes), the local push server on port 3002 (is it authenticated? can a LAN attacker push vouchers to Tally?), CORS config, input validation on the 2992-line sync.ts, and code-signing of the Electron installer (NSIS). Rank by severity. Return per schema with concrete remediations.`,
  },
  {
    label: 'prod:cicd-release',
    prompt: DIGEST_HEADER + `DIMENSION = CI/CD, BUILD, RELEASE & PACKAGING.
There is no CI. Read ${TB}/package.json build/dist/electron-builder config, the build:python PyInstaller command, ${TB}/backend/Dockerfile, ${TB}/parsing/Dockerfile, .gcloudignore files, and ${AIA}/build_test.ps1, .firebaserc, firebase.json. Memory notes: auto-update via electron-builder --publish on a public GitHub repo, GCP Cloud Run deploys done manually (redeploy gotchas: revision serves 0% traffic, push-queue API key breaks), multiple GCP projects (billing 3-project cap), per-client Supabase projects requiring migration fan-out.
Design: GitHub Actions pipelines for (1) Electron build+sign+release+auto-update publish, (2) backend Cloud Run deploy, (3) parsing Cloud Run deploy, (4) Flutter multi-flavor build. Address Windows code-signing, versioning, environment promotion (testing→client prod), and the per-client Supabase migration fan-out problem (recommend a migration tool — supabase CLI migrations / dbmate). Return per schema.`,
  },
  {
    label: 'prod:observability',
    prompt: DIGEST_HEADER + `DIMENSION = OBSERVABILITY, ERROR HANDLING & RESILIENCE.
Read ${TB}/src/main/remote-log.ts (BetterStack forwarder), logger.ts, sync-engine.ts error handling, cloud_pusher.py retry logic, backend/src/index.ts + sync.ts error responses, and the parsing servers' error handling. Memory notes a known client freeze (1.2.11 full-FY voucher catch-up froze single-threaded Tally) and the 6h sync interval mitigation.
Assess: structured logging, crash reporting (is there Sentry? no), health checks on the 3 services, alerting, the swallowed-error patterns (e.g. column-missing 400 swallowed → empty queue), retry/backoff/idempotency on network failures, graceful degradation when Tally/Supabase/RunPod is down, and how a client-side failure becomes visible to the operator. Recommend: Sentry for Electron+backend+Flutter, structured logging standard, Cloud Run health/uptime checks, alerting on sync failures. Return per schema.`,
  },
  {
    label: 'prod:data-tenancy',
    prompt: DIGEST_HEADER + `DIMENSION = DATA INTEGRITY, SCHEMA & MULTI-TENANCY.
Read ${TB}/backend/*.sql (full_schema.sql, supabase_schema_v2..v6.sql, supabase_new_tables.sql, push_queue/scan_jobs sql) and how sync.ts + the tb_ingest_masters RPC upsert. Memory notes serious tenancy pain: one Supabase project per client, migrations must fan out manually, schema DRIFT between yynuu(testing) and ztugw(client prod) caused push_queue.edit_state missing → silent failures; stock part_code rollout pending on some projects.
Assess: idempotency/dedup keys for vouchers & masters, the multiple overlapping schema_v*.sql files (which is source of truth?), migration discipline (none — raw .sql applied by hand), tenant isolation, data-loss risks (render-mode OOM/413 item loss noted), and reconciliation (how do you know cloud == Tally?). Recommend a real migration system, a single source-of-truth schema, a schema-drift CI check across tenant projects, and a reconciliation/audit job. Return per schema.`,
  },
  {
    label: 'prod:code-quality',
    prompt: DIGEST_HEADER + `DIMENSION = CODE QUALITY, MAINTAINABILITY & REPO HYGIENE.
Investigate: the 2992-line backend/src/routes/sync.ts and 1905-line sync_main.py and 933-line cloud_pusher.py monoliths (suggest decomposition), the hand-written .d.ts files alongside .ts in backend/src (build/config smell — is tsc emitting next to source? check tsconfig), the duplicated Guided vs non-Guided renderer pages/components, type safety (any TypeScript strict gaps, untyped Python), linting/formatting (is there eslint/ruff/prettier? check), and REPO HYGIENE — run git status / git ls-files to quantify the dozens of committed log files (backend-*.log, *.err), handoff .md/.html/.pdf docs, .tmp files, and a venv (parsing/.venv-ocrvl) possibly tracked. Recommend: lint+format+typecheck config and pre-commit hooks, .gitignore cleanup, file decomposition priorities, and a CONTRIBUTING/runbook. Return per schema with concrete file evidence.`,
  },
]

const prodResults = (await parallel(prodTasks.map(t => () => agent(t.prompt, { label: t.label, phase: 'Production-Readiness', schema: PROD_SCHEMA })))).filter(Boolean)

return { architecture: archResults, production: prodResults }

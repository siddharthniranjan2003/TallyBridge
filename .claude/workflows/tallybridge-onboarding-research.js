export const meta = {
  name: 'tallybridge-onboarding-research',
  description: 'Deep-read every TallyBridge + aiaccountant subsystem, trace end-to-end flows, critique completeness, for a senior-onboarding HTML doc',
  phases: [
    { title: 'Read Subsystems', detail: '8 parallel deep-readers, one per subsystem across both repos' },
    { title: 'Trace Flows', detail: '5 end-to-end cross-component flow tracers' },
    { title: 'Critique', detail: 'completeness critic over all notes + flows' },
  ],
}

const SUBSYSTEM_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    name: { type: 'string' },
    oneLineSummary: { type: 'string' },
    purpose: { type: 'string', description: '2-4 sentences: what this subsystem is and why it exists' },
    keyFiles: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        properties: { path: { type: 'string' }, role: { type: 'string' } },
        required: ['path', 'role'],
      },
    },
    responsibilities: { type: 'array', items: { type: 'string' } },
    keyComponents: {
      type: 'array',
      description: 'functions / endpoints / classes / screens / services with a short description each',
      items: {
        type: 'object', additionalProperties: false,
        properties: { name: { type: 'string' }, description: { type: 'string' } },
        required: ['name', 'description'],
      },
    },
    talksTo: {
      type: 'array',
      description: 'every external thing this subsystem communicates with',
      items: {
        type: 'object', additionalProperties: false,
        properties: { target: { type: 'string' }, how: { type: 'string' }, direction: { type: 'string' } },
        required: ['target', 'how', 'direction'],
      },
    },
    dataStructures: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        properties: { name: { type: 'string' }, description: { type: 'string' } },
        required: ['name', 'description'],
      },
    },
    configEnv: { type: 'array', items: { type: 'string' } },
    gotchas: { type: 'array', items: { type: 'string' }, description: 'non-obvious design decisions, footguns, things a new senior must know' },
    roleInFlows: { type: 'string', description: 'how this subsystem participates in the sync / push / scan flows' },
  },
  required: ['name', 'oneLineSummary', 'purpose', 'keyFiles', 'responsibilities', 'keyComponents', 'talksTo', 'gotchas', 'roleInFlows'],
}

const FLOW_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    name: { type: 'string' },
    summary: { type: 'string' },
    trigger: { type: 'string', description: 'what kicks this flow off' },
    steps: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        properties: {
          actor: { type: 'string', description: 'component/file responsible for this step' },
          action: { type: 'string' },
          file: { type: 'string', description: 'specific file:function if known, else empty' },
          detail: { type: 'string' },
        },
        required: ['actor', 'action', 'detail'],
      },
    },
    dataShape: { type: 'string', description: 'shape/format of payload as it moves (XML, JSON sections, multipart, etc.)' },
    crossesNetwork: { type: 'array', items: { type: 'string' }, description: 'network hops / ports / URLs traversed' },
    failureModes: { type: 'array', items: { type: 'string' } },
    sequenceText: { type: 'string', description: 'a compact textual sequence diagram, e.g. "A --(HTTP POST /x)--> B --(spawn)--> C"' },
  },
  required: ['name', 'summary', 'trigger', 'steps', 'dataShape', 'sequenceText'],
}

const CRITIC_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    gaps: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        properties: { area: { type: 'string' }, whatsMissing: { type: 'string' }, severity: { type: 'string' } },
        required: ['area', 'whatsMissing', 'severity'],
      },
    },
    corrections: { type: 'array', items: { type: 'string' }, description: 'likely inaccuracies in the gathered notes a senior would catch' },
    onboardingTips: { type: 'array', items: { type: 'string' }, description: 'practical "first day" advice for the senior: how to run, where to start reading, key risks' },
    suggestedSections: { type: 'array', items: { type: 'string' }, description: 'sections the final HTML doc must contain' },
  },
  required: ['gaps', 'onboardingTips', 'suggestedSections'],
}

const TB = 'D:/Desktop/TallyBridge'
const AI = 'D:/Desktop/Ai_Accountant/aiaccountant'

const READERS = [
  {
    label: 'electron-main',
    prompt: `You are documenting the Electron MAIN PROCESS of the TallyBridge desktop app for a senior-engineer onboarding doc. Read these files thoroughly in ${TB}/src/main/: index.ts, sync-engine.ts, local-push-server.ts, push-queue-poller.ts, ipc-handlers.ts, tray.ts, updater.ts, store.ts, preload.ts, logger.ts. Also skim ${TB}/src/main/store.ts for electron-store config keys.
Explain: how the app boots, how it spawns the Python engine, the local HTTP server on port 3002, the push-queue poller loop, IPC channels to the renderer, tray, auto-updater, electron-store config keys (tallyUrl, syncIntervalMinutes, syncPaused, readMode, syncIngestMode, companies[], migration flags). Capture exact port numbers, intervals, and how pause/resume works. Note the hybrid-sync startup migration and the in-app update banner if present.`,
  },
  {
    label: 'renderer-ui',
    prompt: `You are documenting the REACT RENDERER (desktop UI) of TallyBridge. Read ${TB}/src/renderer/App.tsx, ${TB}/src/renderer/main.tsx, and everything under ${TB}/src/renderer/pages/ and ${TB}/src/renderer/components/. Use Glob to list them first.
Explain: what screens/pages exist, what the desktop user sees and configures (company selection, sync settings, read mode, ingest mode, pause/resume, update banner), how the renderer talks to the main process over IPC (via preload bridge). Keep it from a user-workflow perspective: what a TallyBridge desktop operator actually does.`,
  },
  {
    label: 'python-engine',
    prompt: `You are documenting the PYTHON SYNC ENGINE of TallyBridge. Read thoroughly in ${TB}/src/python/: sync_main.py, tally_client.py, cloud_pusher.py, tally_pusher.py, xml_parser.py, odbc_bridge.py, definition_extractor.py, proxy.py, and skim definitions/odbc_sections.json + definitions/structured_sections.json.
Explain: the entry point sync_main.py and how it decides read mode (auto/xml-only/hybrid/shadow) and ingest mode (render/hybrid/direct); how tally_client.py reads TallyPrime over XML-RPC on port 9000 and the ODBC fallback; how xml_parser parses Tally XML; how cloud_pusher.py batches and POSTs sections to the backend (and which sections go direct-to-Supabase in direct/hybrid mode); how tally_pusher.py writes vouchers back to Tally. Capture the section types (groups, ledgers, stock_items, outstanding, financials, vouchers), the part_code/stock handling, and the port-9000 single-threaded-hang caution.`,
  },
  {
    label: 'backend-api',
    prompt: `You are documenting the EXPRESS BACKEND (cloud API) of TallyBridge. Read thoroughly: ${TB}/backend/src/index.ts, ${TB}/backend/src/routes/sync.ts, ${TB}/backend/src/routes/push-invoice.ts, ${TB}/backend/src/routes/push-voucher.ts, ${TB}/backend/src/middleware/auth.ts, ${TB}/backend/src/db/supabase.ts, ${TB}/backend/src/db/firebase.ts, ${TB}/backend/src/db/gcs.ts.
Explain: every HTTP route/endpoint and its purpose, the auth model (Firebase JWT vs API_KEY with timingSafeEqual, and the *_CLIENT env variants), how /api/sync ingests each section type and writes to Supabase (including any RPC like tb_ingest_masters), how push-invoice and push-voucher interact with push_queue, the role of GCS, the multi-tenant routing (one Supabase project per client). Capture port (3001), body limit, and env vars.`,
  },
  {
    label: 'parsing-ocr',
    prompt: `You are documenting the PARSING / INVOICE-OCR pipeline of TallyBridge. Read thoroughly: ${TB}/parsing/purchase/*.py (company_context.py, vendor.py, vlm_vendor_parser.py, voucher_builder.py, models.py), ${TB}/parsing/server/handler.py, ${TB}/parsing/purchase_docstrange_pipeline.py, ${TB}/parsing/purchase_image_pipeline.py, ${TB}/parsing/purchase_ocrvl_pipeline.py. Glob ${TB}/parsing for the runner/server files and skim ${TB}/parsing/minicpm-serverless-worker and ${TB}/parsing/runpod-serverless-worker for the VLM worker setup. Read ${TB}/parsing/purchase/README.md.
Explain: the full scanned-invoice → structured-voucher pipeline. Native PDF (fitz) vs scanned (VLM) routing; the OCR/VLM models used (Nanonets, MiniCPM-V on RunPod/GCP Cloud Run); how a purchase invoice becomes a voucher (vendor matching, company context, stock-item matching, voucher_builder); the server handler entry; how results reach Supabase / push_queue. Capture the GCP Cloud Run + RunPod serverless topology and that SUPABASE_KEY must be service_role.`,
  },
  {
    label: 'flutter-client',
    prompt: `You are documenting the aiaccountant FLUTTER CLIENT (mobile + web app) at ${AI}/lib/. Read: main.dart, core/config.dart, core/constants.dart, core/models.dart, firebase_options.dart, services/api_client.dart, data/scan_uploader.dart, data/scan_jobs_service.dart, data/push_queue_service.dart, data/customers_cache.dart, data/vendors_cache.dart, data/stock_items_cache.dart, data/invoice_image_store.dart, and skim features/auth/*, features/camera/*, features/queue/*, features/report/*, features/history/*, features/profile/*, features/shell/app_shell.dart.
Explain: the end-user mobile workflow — OTP/Firebase login, camera capture of an invoice, upload to the parsing/backend, the scan-jobs polling, the review queue (voucher_detail_sheet, scan_result_sheet), editing parsed vouchers, and pushing approved vouchers to the push_queue (which TallyBridge desktop then pulls into Tally). Capture which backend URLs/Supabase project it talks to directly vs via API, the multi-env flavors (testing/deployment/prod), and how it reads cached masters (customers/vendors/stock).`,
  },
  {
    label: 'data-cloud-topology',
    prompt: `You are documenting the DATA MODEL & CLOUD TOPOLOGY of the TallyBridge system. Read ${TB}/backend/full_schema.sql, then Glob and skim ${TB}/supabase/migrations/*.sql and ${TB}/backend/supabase_*.sql. Look specifically for tables: groups, ledgers, stock_items, outstanding/financials, push_queue (incl edit_state, push_now columns), scan_jobs, and any tenant registry.
Explain: the core Supabase schema and how Tally masters/transactions are stored; the push_queue lifecycle (states: pending/edit_state/push_now/done) that links cloud→Tally; the scan_jobs lifecycle that links scan→parse; the multi-tenant model (one Supabase project per client: yynuu=testing, ztugw=client prod, riplara), the GCP project topology (tallybridge-test-ocr, tallybridge-testing-env, riplara testing/deployment), and RunPod endpoints. Note migration-drift risk (must fan migrations out to every client project). Capture which env points at which Supabase.`,
  },
  {
    label: 'config-build-deploy',
    prompt: `You are documenting CONFIG, BUILD & DEPLOY across both repos for an onboarding doc. Read ${TB}/CLAUDE.md, ${TB}/package.json, ${TB}/backend/package.json, ${TB}/backend/Dockerfile, ${TB}/parsing/Dockerfile, ${TB}/parsing/requirements.txt, ${TB}/src/python/requirements.txt, ${AI}/pubspec.yaml. Glob for *.yml/*.yaml build configs and any electron-builder config.
Explain: how to build & run each component (Electron dev/build/dist, backend dev/build/start, Python dev vs PyInstaller engine bundle, parsing Docker/Cloud Run, Flutter flavors). List ALL environment variables for every component and what they configure (root .env, backend/.env, parsing/.env, *_CLIENT variants, VITE_BACKEND_URL, SUPABASE_URL/SERVICE_KEY, API_KEY, FIREBASE_SERVICE_ACCOUNT_B64). Cover the auto-update release flow (electron-builder --publish, GitHub releases) and the PyInstaller engine (tallybridge-engine.exe). This is the "how do I set up my dev environment" section.`,
  },
]

phase('Read Subsystems')
const subsystems = (await parallel(
  READERS.map((r) => () => agent(r.prompt, { label: r.label, phase: 'Read Subsystems', schema: SUBSYSTEM_SCHEMA }))
)).filter(Boolean)

const notesBlob = JSON.stringify(subsystems)

phase('Trace Flows')
const FLOWS = [
  {
    label: 'flow-sync-tally-to-cloud',
    prompt: `Trace the SYNC flow (TallyPrime -> Cloud Supabase) end-to-end across TallyBridge. Read the real entry points: ${TB}/src/main/sync-engine.ts, ${TB}/src/python/sync_main.py, ${TB}/src/python/tally_client.py, ${TB}/src/python/cloud_pusher.py, ${TB}/backend/src/routes/sync.ts. Produce an accurate step-by-step from "sync timer fires / user triggers sync" through Tally XML-RPC read on port 9000, parsing, batching, POST to backend /api/sync (or direct-to-Supabase in direct/hybrid mode), to rows landing in Supabase. Include ports, the port-9000 preflight check, and ingest-mode branching (render/hybrid/direct).`,
  },
  {
    label: 'flow-push-cloud-to-tally',
    prompt: `Trace the PUSH flow (Cloud -> TallyPrime) end-to-end. Read ${TB}/src/main/push-queue-poller.ts, ${TB}/src/main/local-push-server.ts, ${TB}/backend/src/routes/push-voucher.ts, ${TB}/src/python/tally_pusher.py, and the push_queue schema. Produce a step-by-step from "a voucher row is enqueued in push_queue" through the desktop poller polling the backend, the backend forwarding to the local push server on port 3002, spawning tally_pusher.py, and writing the voucher into TallyPrime on port 9000. Include the push_queue state transitions and how the result/ack flows back.`,
  },
  {
    label: 'flow-scan-to-voucher',
    prompt: `Trace the SCAN -> PARSE -> VOUCHER flow end-to-end across aiaccountant + parsing + backend. Read ${AI}/lib/data/scan_uploader.dart, ${AI}/lib/data/scan_jobs_service.dart, ${AI}/lib/features/camera/camera_screen.dart, ${TB}/backend/src/routes/push-invoice.ts, ${TB}/parsing/server/handler.py, ${TB}/parsing/purchase_docstrange_pipeline.py, ${TB}/parsing/purchase/voucher_builder.py. Produce a step-by-step from "user photographs an invoice in the Flutter app" through upload, the scan_jobs record, OCR/VLM parsing on the parsing server (GCP Cloud Run / RunPod), vendor + stock matching, voucher construction, results stored, the review queue in the app, and approval enqueuing into push_queue (which then feeds the push flow). Note native-PDF vs scanned routing.`,
  },
  {
    label: 'flow-auth-tenancy',
    prompt: `Trace the AUTH + MULTI-TENANCY flow across the whole system. Read ${TB}/backend/src/middleware/auth.ts, ${AI}/lib/firebase_options.dart, ${AI}/lib/features/auth/*, ${AI}/lib/services/api_client.dart, ${AI}/lib/core/config.dart, and the *_CLIENT env handling in ${TB}/backend. Explain how a user authenticates (Firebase OTP in Flutter), how requests are authorized at the backend (Firebase JWT vs API_KEY timingSafeEqual, and *_CLIENT vs default), and how a request is routed to the correct tenant's Supabase project (one project per client: testing yynuu / client-prod ztugw / riplara). Cover how the desktop engine authenticates to the backend too (API_KEY).`,
  },
  {
    label: 'flow-glossary-bigpicture',
    prompt: `Produce the BIG-PICTURE system map and a glossary for a senior new to TallyBridge. Using your own reading of ${TB}/CLAUDE.md plus the subsystem notes provided below, write: (1) a one-paragraph "what is this product" summary aimed at a senior engineer, (2) the list of the 5 deployable components and the network ports/URLs each owns (Electron main, renderer, Python engine, Express backend :3001, local push server :3002, Tally :9000, parsing server), (3) a glossary of domain terms (Tally, voucher, ledger, group, stock item, outstanding, ingest mode, read mode, push_queue, scan_job, tenant/Supabase-per-client). Treat the "steps" array as glossary entries (actor=term, action=short def, detail=longer explanation) and put the big-picture paragraph in summary. SUBSYSTEM NOTES:\n${notesBlob}`,
  },
]

const flows = (await parallel(
  FLOWS.map((f) => () => agent(f.prompt, { label: f.label, phase: 'Trace Flows', schema: FLOW_SCHEMA }))
)).filter(Boolean)

phase('Critique')
const critic = await agent(
  `You are a senior engineer reviewing onboarding research before it becomes an HTML doc for ANOTHER senior joining the TallyBridge project. Below are subsystem notes and flow traces gathered from the codebase. Identify: gaps (important things not covered), likely inaccuracies a senior would catch, practical first-day onboarding tips, and the list of sections the final HTML must contain to be a genuinely useful onboarding document. Be concrete and reference the actual system (TallyBridge desktop + aiaccountant Flutter + parsing OCR + Express backend + Supabase-per-tenant).\n\nSUBSYSTEM NOTES:\n${notesBlob}\n\nFLOW TRACES:\n${JSON.stringify(flows)}`,
  { label: 'completeness-critic', phase: 'Critique', schema: CRITIC_SCHEMA }
)

return { subsystems, flows, critic }
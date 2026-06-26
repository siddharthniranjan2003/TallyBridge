# Session Handout — Riplara Multi-Env Buildout + Supabase Migration

**Date:** 2026-06-12
**Scope:** Stood up two isolated environments (testing.riplara, deployment.riplara) across GCP + RunPod + Firebase + the Flutter app, then started migrating the testing Supabase off the shared `yynuu` to a dedicated project. Several pieces are **done**; the **Supabase cutover is mid-flight** — that's the active task.

---

## 0. TL;DR — what's done vs pending

| Area | State |
|---|---|
| GCP testing+deployment Cloud Run (backend+parsing) | ✅ deployed & validated |
| RunPod endpoints per env (Nanonets+MiniCPM) | ✅ created & validated |
| Firebase multi-env + Flutter flavors (app) | ✅ wired; testing web validated end-to-end |
| RunPod model **baking** (faster cold start) | 🔄 on `bake-models` branch; testing endpoints rebuilding |
| Supabase `yynuu` → new testing project | 🔄 **data migrated & verified**; realtime + edge-fn + **cutover still pending** |
| Deployment env push-queue cross-link | ⚠️ still points at OLD prod backend → writes to CLIENT `ztugw` (needs fix + cleanup) |
| Prod app flavor target | ⚠️ `env/prod.json` backend/Supabase are placeholders pending confirmation |

---

## 1. Environments & identifiers

### GCP projects (gcloud configs: `default`=rohan.psom, `testing-riplara`, `deployment-riplara`)
| Env | Account | GCP project | Number |
|---|---|---|---|
| (old) testing | rohan.psom@gmail.com | `tallybridge-testing-env` | 366926737745 |
| **testing.riplara** | testing.riplara@gmail.com | `tally-bridge-testing-env` | **828647628834** |
| **deployment.riplara** | deployment.riplara@gmail.com | `tally-bridge-deployment-env` | **822222628942** |

Switch: `gcloud config configurations activate <testing-riplara|deployment-riplara|default>`

### Cloud Run URLs (asia-south1)
- testing backend: `https://tallybridge-backend-828647628834.asia-south1.run.app`
- testing parsing: `https://tallybridge-parsing-828647628834.asia-south1.run.app`
- deployment backend: `https://tallybridge-backend-822222628942.asia-south1.run.app`
- deployment parsing: `https://tallybridge-parsing-822222628942.asia-south1.run.app`

### RunPod endpoints (3 isolated sets, each paired with its own `rpa_` key — no 401s)
| Env | Nanonets (purchase) | MiniCPM (sale) |
|---|---|---|
| prod | `vllm-5fjqw6zdgqxt8g` | `vllm-vhm6qdmcjavvps` |
| testing.riplara | `vllm-oubuuwydlz9t7b` | `vllm-l3ra5g9jipnoeu` |
| deployment.riplara | `vllm-e2026vl152kynt` | `vllm-9px3t17t9fn1y7` |

Built from GitHub repos: **`tb-vllm-worker`** (Nanonets, branch `master`) + **`tb-minicpm-vllm-worker`** (MiniCPM, branch `main`), under `siddharthniranjan2003`. Parsing wiring: `RUNPOD_POD_URL`/`RUNPOD_POD_API_KEY` = Nanonets; `MINICPM_RUNPOD_URL`/`MINICPM_RUNPOD_API_KEY` = MiniCPM; timeouts bumped to 2000.

### Firebase projects (per env)
| Env (Flutter flavor) | Firebase project |
|---|---|
| **staging** (= testing; gradle forbids `test*` flavor names) | `tallybridge-testing-env-636d4` (#639712744833) |
| **deployment** | `tallybridge-deployment-env` (#101785570606) |
| **prod** | `aiaccountant-b60ed` (#845558047543) |

Firebase CLI has all 3 Google accounts; deploy web with `--account <email>` matching the project. App package (all flavors, base): `com.example.aiaccountant` (no side-by-side installs).

### Supabase
- `yynuu` = `yynuuysvjeipawzfbeme` — **TESTING** (shared, being migrated away from). Region ap-northeast-1.
- `ztugw` = `ztugwhevemibdrzqafyw` — **CLIENT/prod**.
- **NEW testing.riplara Supabase** = `rfwjpflkfqsccpukraed` (Singapore ap-southeast-1, NANO), URL `https://rfwjpflkfqsccpukraed.supabase.co`, anon/publishable key starts `sb_publishable_kXyBkVm8L4l_uSG...`.

---

## 2. Flutter app multi-env (aiaccountant) — DONE, on branch `project_reorg`, NOT committed

Repo `D:\Desktop\Ai_Accountant\aiaccountant`. Flavors replace hand-editing `config.dart` + swapping `google-services.json`.

Files created/edited:
- `android/app/build.gradle.kts` — `flavorDimensions "env"` + flavors `staging`/`deployment`/`prod` (same base package).
- `android/app/src/<flavor>/google-services.json` — per-env Firebase config.
- `lib/firebase_options.dart` — switches android+web options on `String.fromEnvironment('FLAVOR')`.
- `lib/core/config.dart` — reads URLs/keys from `String.fromEnvironment(...)` (defaults = testing/828647628834+yynuu).
- `env/testing.json`, `env/deployment.json`, `env/prod.json` — per-env values (each sets `FLAVOR`).
- `.firebaserc` — aliases `testing`/`deployment`/`prod`.

**Build/deploy commands (PowerShell, no `&&`):**
```
# TESTING (flavor "staging", env/testing.json)
flutter build web --dart-define-from-file=env/testing.json
firebase deploy --only hosting --project testing --account testing.riplara@gmail.com    # -> tallybridge-testing-env-636d4.web.app
flutter build apk --flavor staging --dart-define-from-file=env/testing.json             # -> app-staging-release.apk

# DEPLOYMENT
flutter build web --dart-define-from-file=env/deployment.json
firebase deploy --only hosting --project deployment --account deployment.riplara@gmail.com   # -> tallybridge-deployment-env.web.app
flutter build apk --flavor deployment --dart-define-from-file=env/deployment.json
```
Web build + its deploy must be paired (`build/web` is shared). Backend `FIREBASE_SERVICE_ACCOUNT_B64`: testing (828647628834) set to `tallybridge-testing-env-636d4` admin SDK & **verified**; deployment backend command given but **not confirmed run**.

**Validated:** testing web app live, phone-OTP login works (after fixing SMS region policy → Allow India), voucher queue loads from testing backend+Supabase.

**Gotchas:** new Firebase project blocks SMS until **Authentication → Settings → SMS region policy → Allow → India**; gradle flavor can't start with `test` (hence `staging`); firebase CLI is separate from gcloud auth.

---

## 3. RunPod baking (faster cold start) — IN PROGRESS

Added to each worker Dockerfile (after `pip install ... runpod requests`), on a **`bake-models`** branch in both repos:
```dockerfile
# Nanonets (tb-vllm-worker)
RUN python3 -c "from huggingface_hub import snapshot_download; snapshot_download('nanonets/Nanonets-OCR2-3B')"
# MiniCPM (tb-minicpm-vllm-worker)
RUN python3 -c "from huggingface_hub import snapshot_download; snapshot_download('openbmb/MiniCPM-V-4_5')"
```
(Uses already-installed `huggingface_hub` — do NOT `pip install huggingface_hub[cli]`, it can bump the pinned vLLM.)

**Isolation:** only the **testing** endpoints were pointed at `bake-models` (prod/deployment stay on master/main → untouched). RunPod auto-builds on push to the tracked branch, so a branch keeps it testing-only. Hit **"Unable to find repo"** when typing the new branch → cause = RunPod's stale branch cache; fixed by refreshing the GitHub connection (then the Branch field became a dropdown showing `bake-models`). The "No credentials / +" in Repository config is a **Docker-registry** cred, NOT GitHub — leave it.

**What baking does:** the model weights become a layer inside the image (built once, slow build), so worker cold starts skip the HF download (~7 GB / ~17 GB). Build happens on RunPod's builder (Builds tab), not at request time. Still ~30–90s to load weights into GPU per cold start (only min-worker=1 removes that).

**Other speedups discussed (not done):** idle timeout 5s→60–300s; `--enforce-eager` + `--enable-prefix-caching` in handler.py; lower `MAX_MODEL_LEN`; quantization; min-workers=1 (zero cold start, 24/7 cost) or business-hours-warm.

---

## 4. Supabase migration `yynuu` → `rfwjpflkfqsccpukraed` — DATA DONE, CUTOVER PENDING

### Tooling reality
- `supabase db dump` needs **Docker** (not installed) → used **native pg_dump 17** instead (installed via `winget install PostgreSQL.PostgreSQL.17`; add `C:\Program Files\PostgreSQL\17\bin` to PATH).
- Use **Session pooler** connection (port 5432, IPv4); Direct connection is IPv6-only. Reset DB passwords to **alphanumeric** (special chars `@ : / # ?` break the URI).
- `$SRC` (yynuu) and `$DST` (new) are PowerShell vars holding the session-pooler URIs with passwords.

### Commands that worked
```powershell
# dump public only (native pg_dump, no Docker)
pg_dump --schema=public --no-owner --dbname $SRC --file public_dump.sql
# restore (drop empty public first; NO --single-transaction/ON_ERROR_STOP so harmless errors are skipped)
psql --dbname $DST --command "drop schema if exists public cascade;"
psql --file public_dump.sql --dbname $DST
```
The **only** restore errors were `ALTER DEFAULT PRIVILEGES ... permission denied` (sets defaults for future objects on a role `postgres` can't touch) — **harmless**; new project already has Supabase's default-privilege setup. With `--single-transaction` those errors roll back EVERYTHING, so it was re-run WITHOUT it.

### Data verified — all counts match source exactly
`vouchers 45060 · voucher_items 215929 · voucher_ledger_entries 146567 · stock_items 13237 · ledgers 2904 · purchases 5933 · push_queue 108` (+ Purchase_Matching 321, etc.).

### yynuu feature audit (so we know what else to move)
- public: **15 tables, 1 view, 10 functions, 1 policy, 0 triggers**, **RLS OFF on all 15** (matches).
- **auth_users 0, storage 0, pg_cron not installed** → nothing to migrate there (app uses Firebase).
- Extensions: pgcrypto, uuid-ossp, pg_stat_statements, supabase_vault, plpgsql (restore succeeded → new project has what's needed).
- **Realtime: 1 table** published to `supabase_realtime` (NOT in the public dump). Likely `push_queue` (live queue view) — must confirm + re-add.
- **Edge function `ingest-sync`** (ACTIVE v4) — separate deploy.

### REMAINING migration steps
1. **Realtime** (pure SQL; run in new project's SQL Editor or `psql $DST`). First identify the table:
   ```sql
   select schemaname, tablename from pg_publication_tables where pubname='supabase_realtime';   -- on $SRC
   ```
   then on the new DB: `alter publication supabase_realtime add table public.<that_table>;`
2. **Edge function** (CLI only — not SQL; run from repo root, logged in, `--use-api` avoids Docker):
   ```powershell
   cd D:\Desktop\TallyBridge
   supabase functions deploy ingest-sync --project-ref rfwjpflkfqsccpukraed --use-api
   supabase secrets set SYNC_INGEST_KEY=<value matching the caller> --project-ref rfwjpflkfqsccpukraed
   ```
3. **CUTOVER — repoint testing env at the new DB** (need new project's `service_role` + anon keys from Settings → API):
   - testing **backend** (828647628834): `SUPABASE_URL=https://rfwjpflkfqsccpukraed.supabase.co`, `SUPABASE_SERVICE_KEY=<service_role>`
   - testing **parsing** (828647628834): same two + `SUPABASE_KEY=<service_role>` (⚠️ MUST be service_role, not anon — anon silently returns `[]`)
   - app `env/testing.json` + `config.dart` defaults: `supabaseUrl` + `supabaseAnonKey` → new project, then rebuild/redeploy testing web + APK.
   - `gcloud run services update ... --update-env-vars "^|^KEY=val|KEY2=val2"` (use `^|^` delimiter; active config must be `testing-riplara`).
   - NOTE: backend `*_CLIENT` Supabase vars (ztugw) left untouched — separate client path.

---

## 5. Outstanding cleanup / open items
- **Deployment push-queue cross-link bug:** deployment parsing's `MINICPM_PUSH_QUEUE_URL` still points at the OLD prod backend `tallybridge-backend-950406969086...` → its enqueues land in the **CLIENT `ztugw`** push_queue. Two test jobs are pending there: `a5a11448-727b-4e4a-a322-ebd2103d8cb9`, `8e2c99f4-c935-4dc6-83eb-99bfee9927e6` → should be `update push_queue set status='failed'` (or deleted) on `ztugw`, and the URL repointed to the deployment backend. (testing.riplara correctly writes to yynuu — verified; only deployment is mis-linked.)
- **Deployment backend `FIREBASE_SERVICE_ACCOUNT_B64`**: command given, not confirmed run (tallybridge-deployment-env admin SDK → 822222628942 backend).
- **Deployment Firebase**: still needs Phone provider + SMS region → India before its app login works.
- **Prod app flavor** (`env/prod.json`): Firebase correct (aiaccountant-b60ed) but backend/Supabase are placeholders (366926737745) pending "what is prod" decision.
- **RunPod baking**: finish rebuilding both testing endpoints from `bake-models`, validate faster cold start, then later merge to master/main + rebuild deployment/prod.

---

## 6. Memory files written this session (in `…/memory/`)
- `riplara_testing_deployment_envs.md` — GCP/RunPod for both riplara envs.
- `aiaccountant_multienv_flavors.md` — flavor system, per-env Firebase, build commands, gotchas.
(Existing relevant: `supabase_db_identity.md`, `parsing_supabase_service_role_key.md`, `minicpm_v45_serverless_worker.md`, `purchase_ocr_serverless_config.md`, `gcp_testing_env_project.md`.)

## 7. One-line state
Testing.riplara is fully built (GCP+RunPod+Firebase+app, validated); its Supabase data is **migrated & verified** into `rfwjpflkfqsccpukraed` but **not yet cut over** — finish realtime + `ingest-sync` deploy, then repoint backend/parsing/app keys+URL and rebuild the app. Deployment.riplara is built but has an un-fixed push-queue→client cross-link and incomplete Firebase. Prod app flavor target is unconfirmed.

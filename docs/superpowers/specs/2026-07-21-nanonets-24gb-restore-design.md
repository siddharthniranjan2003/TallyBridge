# Restore Nanonets Purchase OCR on a 24GB Card

**Date:** 2026-07-21
**Endpoint:** `vllm-oubuuwydlz9t7b` (`tb-vllm-worker-test-env`, account `testing.riplara`)
**Status:** Design approved, pending implementation

## Context

TallyBridge's purchase-invoice OCR (Nanonets-OCR2-3B on a RunPod serverless endpoint)
has been down. Sale OCR (MiniCPM-V-4.5) is unaffected and working on both accounts.

Three distinct faults were diagnosed, in the order they appeared:

**1. Worker never booted.** RunPod began injecting Blackwell
`RTX PRO 6000 Blackwell Server Edition MIG 1g.24gb` slices into the 24GB GPU tier.
This is deliberate and documented — RunPod's MIG blog (updated 2026-07-13) states they
are "implementing MIG for Serverless endpoints, which will bolster the supply in the
highly sought after 24 GB spec." vLLM v0.9.2 has no Blackwell (SM120) CUDA kernels, so
workers hung at `INITIALIZING` indefinitely with no log output and no unhealthy signal.

**2. Garbage output (self-inflicted).** To get Blackwell kernels, the base image was
bumped v0.9.2 → v0.25.1. Workers then booted but returned a stream of `!` instead of
OCR text. Root cause: **vLLM >= 0.11 regressed Qwen2.5-VL decoding** — the `!` is token
id 0, what `argmax` collapses to when logits go NaN (upstream
[vllm#27775](https://github.com/vllm-project/vllm/issues/27775),
[#14126](https://github.com/vllm-project/vllm/issues/14126)). Nanonets-OCR2-3B is a
Qwen2.5-VL-3B-Instruct fine-tune (`config.json` declares
`Qwen2_5_VLForConditionalGeneration`), so it is affected. MiniCPM-V-4.5 is a different
architecture, which is why sale OCR was never hit by this.

Corroborated externally: HuggingFace's `uv-scripts/ocr` collection ships a
Nanonets-OCR2-3B recipe pinned to a v0.10.2 image with the same rationale, after trying
`>=0.15.1` and reverting. v0.9.2 / v0.10.1 / v0.10.2 are the known-good versions.
v0.24.0 and v0.25.1 were both tested here and produced garbage on **both** Blackwell and
real Ampere, and on text-only prompts with no image — confirming a decode regression,
not a hardware or vision-encoder fault.

**3. Rebuilt v0.9.2 image does not work (current blocker).** Reverting the Dockerfile to
v0.9.2 and rebuilding produced an image that boots correctly in isolation but will not
serve jobs on RunPod. Observed: workers reach `IDLE`/`READY` on a real `NVIDIA A40`, yet
a freshly submitted job sits `IN_QUEUE` with `inProgress: 0` forever — even a tiny
text-only prompt. `completed` has been frozen at 964 throughout.

The mechanism: in `handler.py`, vLLM is started and awaited at module import
(`_start_vllm()` then `_wait_ready(timeout=1200)`) **before** `runpod.serverless.start()`
is reached. RunPod reports the container healthy, but the handler is still blocked in
`_wait_ready()`, so the worker never registers to pull jobs. At 1200s it raises and the
container restarts — matching the ~200–400s worker uptimes and repeated respawns seen.

The rebuilt image was verified good in isolation (run locally on a GPU): vLLM v0.9.2
parsed args, initialised the V1 engine, loaded the model in ~14s from the baked cache,
and selected the xformers vision backend — matching the historical working logs. It only
failed at KV-cache allocation because the local GPU has 10GB. So the image is not
*broken*, but it differs from the original in ways that evidently matter:

- Added `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1` env (not in the original)
- Its `snapshot_download` pulled ~15GB; vLLM logs
  `Using model weights format ['*.safetensors', '*.bin', '*.pt']` where the original
  logged only `['*.safetensors']`
- Served from `ghcr.io` rather than `registry.runpod.net`, and is much larger
  (22.5GB compressed / ~58GB unpacked vs the original's smaller footprint)

Decisive comparison: **the original image reached `vLLM ready` in 21 seconds**
(per worker logs from 2026-07-16). The rebuild never gets there.

### Intended outcome

Purchase scanning works again — a real invoice returns correct OCR text — running on the
cheaper 24GB tier without risk of landing on Blackwell hardware.

### Already ruled out (do not retry)

`--enforce-eager` on/off · sampling params (temperature, `repetition_penalty`) · image
DPI / prompt-token budget (300 vs 150 DPI) · KV/prefix cache pollution (tested on a
brand-new worker's first-ever request) · model weights changing upstream (HF weights
unchanged since 2025-10-13; all later commits are README-only) · vLLM v0.24.0 · v0.25.1.
The repo Dockerfile also already flagged v0.15–v0.20 as bad for this model.

## Approach

Stop trying to reproduce a working image and go back to the one that demonstrably worked,
then make the 24GB tier structurally safe.

### 1. Revert to the known-good image

Point the endpoint at
`registry.runpod.net/siddharthniranjan2003-tb-vllm-worker-bake-models-dockerfile:3c1ee2c7b`
— the v0.9.2 build that served 950+ jobs and booted in 21s. Abandon the
`ghcr.io/...:v0-9-2-ampere` rebuild.

Cross-account/registry pulls of `registry.runpod.net` images are known to work: the same
technique was used earlier today to fix deployment.riplara's MiniCPM endpoint by
repointing its template at an image built under a different account.

### 2. Undo the endpoint-level changes made while debugging

- Remove the `HF_HUB_OFFLINE=0` / `TRANSFORMERS_OFFLINE=0` env overrides
- Restore `containerDiskInGb` to **35** (the 80GB bump was for the oversized rebuild)

Goal: return the endpoint to its known-working configuration rather than a hybrid.

### 3. Make the 24GB tier safe

The user unticks the MIG opt-out in the RunPod console
(endpoint → **Advanced** → uncheck the Pro 6000 MIG spec). This is the only officially
stated opt-out and there is **no API field for it** in v1, v2, or GraphQL — it is
console-only, so it cannot be automated.

Then set `gpuPoolIds: ["AMPERE_24"]`, matching the original working config, whose worker
env showed `RUNPOD_GPU_SIZE: "AMPERE_24,ADA_24"`.

Why the opt-out is required: `AMPERE_24` alone is *not* sufficient. A brand-new endpoint
created with `gpuPoolIds: ["AMPERE_24"]` was still assigned the Blackwell MIG slice, as
was an endpoint restricted by explicit GPU names (A5000/L4/3090). Only a 48GB request
structurally excluded it — and even that is not durable, since
`RTX PRO 6000 Blackwell ... MIG 2g.48gb` also exists in the catalog.

### 4. Prevent future regressions in the repo

Repo: `siddharthniranjan2003/tb-vllm-worker`, branch `bake-models`.

- Remove the `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` lines added during debugging so
  the recipe matches the proven original
- Keep the "DO NOT UPGRADE vLLM PAST v0.10.2" header documenting the Qwen2.5-VL decode
  regression, so the next person doesn't reintroduce fault #2

## Files and resources

| Resource | Role |
|---|---|
| RunPod endpoint `vllm-oubuuwydlz9t7b` | Target; config changes only, no rebuild |
| `registry.runpod.net/...tb-vllm-worker-bake-models-dockerfile:3c1ee2c7b` | Known-good image to restore |
| `tb-vllm-worker` repo, `bake-models` branch — `Dockerfile` | Drop offline env vars; keep version-pin warning |
| `tb-vllm-worker` repo, `bake-models` branch — `handler.py` | No change; `--enforce-eager` already restored |
| `…\scratchpad\nanonets_test_payload_150.json` | Prebuilt test payload — real `cp.pdf` invoice at 150 DPI |

## Verification

Each step gates the next; do not skip ahead on partial success.

1. **Image pulls.** Confirm `3c1ee2c7b` still exists in RunPod's registry and a worker
   starts from it. *If garbage-collected, see Fallbacks.*
2. **Correct hardware.** `mcp__runpod__list-endpoint-workers` shows `gpuTypeId` of a real
   Ampere 24GB card (A5000 / RTX 3090 / L4) — never `RTX PRO 6000 ... MIG`.
3. **Worker claims a job.** The current failure mode. Purge the queue, submit a fresh job,
   and confirm `inProgress` goes to 1 and `completed` rises above **964**. Always purge
   and resubmit after any config change — jobs queued under an older endpoint version
   appear to be orphaned and never routed.
4. **Coherent decoding.** Tiny text-only `runsync`:
   `{"input":{"model":"nanonets/Nanonets-OCR2-3B","messages":[{"role":"user","content":"say hello"}],"max_tokens":20}}`
   → must return real text. If it returns `!!!!` or gibberish, v0.9.2 does **not** fix the
   decode regression and that is a major finding — stop and report.
5. **End-to-end invoice.** POST `nanonets_test_payload_150.json` to
   `https://api.runpod.ai/v2/vllm-oubuuwydlz9t7b/run`, poll `/status/{id}`.
   - **PASS:** `status: COMPLETED`, `finish_reason: "stop"`, and `content` contains real
     invoice text (vendor name, line items, amounts).
   - **FAIL:** `content` is `!!!!`/gibberish, or `finish_reason: "length"`.

Expect a cold start in the low hundreds of seconds; the original booted to ready in 21s
once the image was cached.

## Fallbacks

- **`3c1ee2c7b` no longer in the registry:** rebuild from the exact original recipe — no
  offline env vars, nothing added — and push. Verify the boot log shows
  `Using model weights format ['*.safetensors']` (not `[..., '*.bin', '*.pt']`), which
  distinguishes it from the failed rebuild.
- **24GB Ampere supply too throttled:** all three real Ampere-24 types report `LOW`
  availability. `AMPERE_48` (real A40, proven to land correctly) remains available at
  ~44% higher cost (~$1.22/hr vs ~$0.68/hr serverless tier pricing).
- **MIG opt-out checkbox absent from the console:** fall back to datacenter fencing — pin
  `AMPERE_24` plus `dataCenterIds` limited to DCs with Ampere-24 supply but no
  RTX PRO 6000: `CA-MTL-1` (A5000), `EU-CZ-1` (3090), `US-GA-2` (L4). Weaker guarantee:
  v2 treats `dataCenterIds` as *preferred*, not strict, and DC inventory shifts over time.

## Out of scope

- **deployment.riplara's Nanonets endpoint** (`vllm-e2026vl152kynt`) — not examined. Once
  this is verified, the same fix should transfer by repointing its template at the
  working image.
- **Running Nanonets on Blackwell.** No publicly confirmed vLLM version serves this model
  correctly on SM120. v0.15–0.20 flagged bad in-repo; v0.24/v0.25 confirmed bad here;
  v0.13/v0.14/v0.21–v0.23 untested (research's best single candidate: **v0.14.1**, which
  is post-#27744, post-#30125 ViT refactor, has SM120 kernels, and sits below all the
  Qwen2.5-VL ViT CUDA-graph/FlashInfer changes in v0.21+). Not required — nothing about
  this workload needs Blackwell; it was only ever forced on us by the scheduler.

## Known RunPod issues worth reporting

- The GPU catalog reports `pool: null` for the MIG SKU (documented as "Null if GPU is not
  in a serverless pool") while it is demonstrably scheduled into the 24GB pool.
- The MIG opt-out exists only in the console with no corresponding API field.

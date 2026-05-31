# Custom RunPod serverless worker — vLLM 0.10.2 for MiniCPM-V 4.5

Serves `openbmb/MiniCPM-V-4_5` (8.7B vision-language model) behind an
OpenAI-compatible RunPod serverless endpoint. Intended to replace the local
Ollama MiniCPM call in the **sale** OCR path (`run_pipeline_for_image`).

## Why a separate worker from the Nanonets one

| Model | Required vLLM | Worker |
|---|---|---|
| `nanonets/Nanonets-OCR2-3B` | **0.9.2 only** (≥0.15 crash/garbage) | `parsing/runpod-serverless-worker/` |
| `openbmb/MiniCPM-V-4_5` | **≥ 0.10.2** (merged 2025-08-27) | this folder |

On vLLM < 0.10.2 MiniCPM-V 4.5 fails with:
`ValueError: MiniCPMV only supports versions 2.0, 2.5, 2.6, 4.0. Got version: (4, 5)`.
The two pins are mutually exclusive, so the models cannot share an image.

## What's here
- `Dockerfile` — `FROM vllm/vllm-openai:v0.10.2` + `runpod` handler.
- `handler.py` — boots vLLM on 127.0.0.1:8000, proxies RunPod jobs (OpenAI route + native).
  Defaults `MODEL_NAME=openbmb/MiniCPM-V-4_5`, `--trust-remote-code`, 1 image/prompt.

## Deploy (RunPod builds from GitHub — no local Docker)
1. Create a new GitHub repo `tb-minicpm-vllm-worker` and push the **contents of this
   folder** to its root (`handler.py` + `Dockerfile` at repo root):
   ```bash
   gh repo create tb-minicpm-vllm-worker --public --source=. --remote=origin
   # (run from a clean checkout containing only these two files at the root)
   git add Dockerfile handler.py README.md
   git commit -m "MiniCPM-V 4.5 RunPod serverless worker (vLLM 0.10.2)"
   git push -u origin main
   ```
2. RunPod → Serverless → New Endpoint → **Import from GitHub** → select
   `tb-minicpm-vllm-worker` / `main`.
3. Environment variables:
   - `MODEL_NAME=openbmb/MiniCPM-V-4_5`
   - `MAX_MODEL_LEN=16384`
   - `GPU_MEMORY_UTILIZATION=0.9`
   - `DTYPE=bfloat16`
   - `MAX_IMAGES_PER_PROMPT=1`
4. GPU: **48 GB** (L40S / A6000). 8.7B bf16 ≈ 17 GB weights; HF recommends ~28 GB
   total, so 24 GB (L4/A5000) is too tight for a 16k context — use 48 GB for
   reliable cold starts. **Network volume: none. Data centers: all.** (network
   volume pins one DC → throttling; full-access `rpa_` API key, not a restricted one.)
5. Workers: min 0, max 1–2, idle timeout 60–120s, FlashBoot on.

## Client wiring (sale path)

The sale path currently calls local Ollama in the pipeline module
(`query_ollama_chat` / `query_ollama_text`, used by `run_pipeline_for_image` in
`parsing/server/handler.py`). To point it at this endpoint, send an
OpenAI chat-completions body to RunPod — model `openbmb/MiniCPM-V-4_5`, the image
as a base64 `image_url`, e.g. `data:image/jpeg;base64,<...>`.

Two transports, both handled by `handler.py`:
- OpenAI route: `POST https://api.runpod.ai/v2/<ENDPOINT_ID>/openai/v1/chat/completions`
- Native:       `POST https://api.runpod.ai/v2/<ENDPOINT_ID>/runsync` with
  `{"input": <openai chat body>}`

The existing Nanonets RunPod call helper (`call_runpod_markdown_ocr` in
`parsing/server/handler.py`) is the working reference for auth headers, the
base64 image payload, and the `/run`+`/status` fallback when the OpenAI route
isn't proxied. Suggested `.env` knobs (separate from the Nanonets ones so both
endpoints can coexist):
```
MINICPM_RUNPOD_URL=https://api.runpod.ai/v2/<ENDPOINT_ID>/openai
MINICPM_RUNPOD_API_KEY=<full-access rpa_ key>
MINICPM_RUNPOD_MODEL=openbmb/MiniCPM-V-4_5
MINICPM_RUNPOD_TIMEOUT_SECONDS=300
```

## Notes
- First cold start downloads the ~17 GB model unless baked into the image or a
  network volume is attached.
- After confirming the endpoint serves correctly, rewiring the sale path to call
  it (instead of Ollama) is a separate code change in the pipeline module — not
  done yet.

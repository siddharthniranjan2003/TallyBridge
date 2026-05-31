# Custom RunPod serverless worker — vLLM 0.9.2 for Nanonets-OCR2-3B

Prebuilt RunPod `worker-vllm` is broken for `nanonets/Nanonets-OCR2-3B` — and every
release maps to a too-new vLLM:

| worker-vllm | vLLM | result for this model |
|---|---|---|
| v2.19 / `main` | 0.20.2 | crash (`lm_head not initialized`) / garbage `!!!!` |
| v2.16 | 0.17.1 | generation hangs / worker unhealthy |
| v2.13.1 | 0.15.1 | (untested, still 0.15+) |

Only **vLLM 0.9.2** serves this model correctly (proven on a pod). This worker pins
that version and exposes OpenAI-compatible serving behind the RunPod serverless handler.

## What's here
- `Dockerfile` — `FROM vllm/vllm-openai:v0.9.2` + `runpod` handler.
- `handler.py` — boots vLLM on 127.0.0.1:8000, proxies RunPod jobs (OpenAI route + native).

## Deploy (Option A — RunPod builds from GitHub, no local Docker)
1. Push this folder to a GitHub repo (handler.py + Dockerfile at repo root).
2. RunPod -> Serverless -> New Endpoint -> Import from GitHub -> select the repo/branch.
3. Environment variables:
   - `MODEL_NAME=nanonets/Nanonets-OCR2-3B`
   - `MAX_MODEL_LEN=16384`
   - `GPU_MEMORY_UTILIZATION=0.9`
   - `DTYPE=bfloat16`
4. GPU: **24 GB** (L4 / A5000). **Network volume: none.** **Data centers: all.**
5. Workers: min 0, max 1-2, idle timeout 60-120s, FlashBoot on.

## Deploy (Option B — build + push yourself)
```bash
docker build -t <your-dockerhub>/tb-vllm092:latest parsing/runpod-serverless-worker
docker push <your-dockerhub>/tb-vllm092:latest
```
Then create the endpoint with that image + the env/GPU settings above.

## Client wiring (`parsing/.env`)
```
RUNPOD_POD_URL=https://api.runpod.ai/v2/<ENDPOINT_ID>/openai
RUNPOD_POD_API_KEY=<RunPod API key>
RUNPOD_MODEL=nanonets/Nanonets-OCR2-3B
RUNPOD_TIMEOUT_SECONDS=300
```
The existing handler posts to `{RUNPOD_POD_URL}/v1/chat/completions`. If the OpenAI
route doesn't pass through to the worker, switch the client to `/runsync` with
`{"input": <openai body>}` (the handler's native branch handles it).

## Notes
- First cold start downloads the ~7 GB model unless a network volume / baked model is used.
- To cut cold starts later, bake the model into the image at build time or attach a
  network volume in a high-supply datacenter.
```

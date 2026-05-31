"""RunPod serverless handler for vLLM serving openbmb/MiniCPM-V-4_5.

Boots the vLLM OpenAI API server in-container (127.0.0.1:8000), waits for it to
be ready, then serves RunPod jobs by proxying to it. Supports both:

  - RunPod OpenAI compatibility: POST /v2/<id>/openai/v1/chat/completions
      -> RunPod delivers job["input"] = {"openai_route": "...", "openai_input": {...}}
  - Native: POST /v2/<id>/runsync with {"input": <openai chat body>}

Config via env (set on the endpoint):
  MODEL_NAME (default openbmb/MiniCPM-V-4_5), MAX_MODEL_LEN (16384),
  GPU_MEMORY_UTILIZATION (0.9), DTYPE (bfloat16), MAX_IMAGES_PER_PROMPT (1),
  VLLM_STARTUP_TIMEOUT (1200).
"""
import os
import subprocess
import time

import requests
import runpod

MODEL_NAME = os.environ.get("MODEL_NAME", "openbmb/MiniCPM-V-4_5")
MAX_MODEL_LEN = os.environ.get("MAX_MODEL_LEN", "16384")
GPU_MEMORY_UTILIZATION = os.environ.get("GPU_MEMORY_UTILIZATION", "0.9")
DTYPE = os.environ.get("DTYPE", "bfloat16")
MAX_IMAGES_PER_PROMPT = os.environ.get("MAX_IMAGES_PER_PROMPT", "1")
STARTUP_TIMEOUT = int(os.environ.get("VLLM_STARTUP_TIMEOUT", "1200"))
PORT = "8000"
BASE = f"http://127.0.0.1:{PORT}"


def _start_vllm() -> subprocess.Popen:
    args = [
        "python3", "-m", "vllm.entrypoints.openai.api_server",
        "--model", MODEL_NAME,
        "--served-model-name", MODEL_NAME,
        "--host", "127.0.0.1",
        "--port", PORT,
        "--max-model-len", MAX_MODEL_LEN,
        "--gpu-memory-utilization", GPU_MEMORY_UTILIZATION,
        "--dtype", DTYPE,
        # MiniCPM-V ships custom modeling code; required for the 4.5 architecture.
        "--trust-remote-code",
        # Cap images per request so vLLM sizes the multimodal cache predictably.
        "--limit-mm-per-prompt", f'{{"image": {MAX_IMAGES_PER_PROMPT}}}',
        "--enforce-eager",
    ]
    print(f"[worker] launching vLLM: {' '.join(args)}", flush=True)
    return subprocess.Popen(args)


def _wait_ready(timeout: int) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if requests.get(f"{BASE}/v1/models", timeout=5).ok:
                print("[worker] vLLM ready", flush=True)
                return
        except Exception:
            pass
        time.sleep(3)
    raise RuntimeError("vLLM did not become ready within timeout")


_VLLM_PROC = _start_vllm()
_wait_ready(STARTUP_TIMEOUT)


def handler(job):
    payload = job.get("input", {}) or {}
    # RunPod OpenAI-compatibility wrapper
    route = payload.get("openai_route")
    if route:
        body = payload.get("openai_input", {}) or {}
        resp = requests.post(f"{BASE}{route}", json=body, timeout=600)
        return resp.json()
    # Native: treat the input itself as an OpenAI chat-completions body
    resp = requests.post(f"{BASE}/v1/chat/completions", json=payload, timeout=600)
    return resp.json()


runpod.serverless.start({"handler": handler})

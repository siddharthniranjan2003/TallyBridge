# PaddleOCR-VL WSL2 + vLLM Setup Handout

This note captures what was done to move the `ocr=vlm` purchase path off the native Windows PaddleOCR-VL runtime and onto a WSL2 + Docker + vLLM-backed server.

## Why We Did This

- The native Windows `ocr=vlm` route was extremely slow.
- Observed timings discussed during debugging:
  - Run 1: about `279s`
  - Run 2: about `226s`
  - Run 3: about `136s`
- The main conclusion was that the native path was not behaving like a healthy GPU-accelerated deployment.
- The codebase already had support for delegating the VL generation stage to a remote/local vLLM server:
  - [paddleocr_vl_server.py](/D:/Desktop/TallyBridge/parsing/paddleocr_vl_server.py:40)
  - [paddleocr_vl_server.py](/D:/Desktop/TallyBridge/parsing/paddleocr_vl_server.py:56)

## Important Architecture Notes

- The main purchase endpoint is:
  - `POST /?type=purchase&company=K%20V%20ENTERPRISES&check=duplicacy&ocr=vlm`
- The request flow is:
  1. Windows main server receives the upload.
  2. `type=purchase&ocr=vlm` routes into `purchase_ocrvl_pipeline.py`.
  3. That calls the local VLM bridge `paddleocr_vl_server.py`.
  4. The VLM bridge either:
     - runs native PaddleOCR-VL in-process, or
     - delegates recognition to a vLLM-compatible server when `MINICPM_VLM_BACKEND=vllm-server`.
- The endpoint does **not** run two full VLM passes for `check=duplicacy`; it reuses the first result.

## Current Code Changes

- Updated [parsing/.env](/D:/Desktop/TallyBridge/parsing/.env:10):

```env
MINICPM_VLM_BACKEND=vllm-server
MINICPM_VLM_VLLM_URL=http://127.0.0.1:8118/v1
```

## WSL / Ubuntu Setup Completed

- Verified `wsl.exe` exists on Windows.
- Confirmed WSL2 is available.
- Installed Ubuntu under WSL2.
- Completed first-launch Ubuntu setup interactively.
- Verified GPU passthrough works inside Ubuntu with `nvidia-smi`.

Observed inside Ubuntu:

- NVIDIA GPU visible in WSL2
- RTX 3080 detected
- CUDA reported by `nvidia-smi`

## Python / Local venv Attempts

Inside Ubuntu:

- System Python was `3.14.4`
- `python3.12` was not available from `apt` on this Ubuntu image
- Installed `uv`
- Installed Python `3.12.13` via `uv`
- Created a venv:

```bash
uv venv --python 3.12 ~/venvs/paddlevl
source ~/venvs/paddlevl/bin/activate
python -m ensurepip --upgrade
python -m pip install --upgrade pip setuptools wheel
```

This gave a working Python 3.12 environment.

## Local PaddleX Installer Attempt

Attempted:

```bash
python -m pip install "paddlex[ocr]"
paddlex --install genai-vllm-server
```

Result:

- Most dependencies installed successfully
- The process failed on `flash-attn==2.8.2`
- The specific failure was during build isolation:
  - `ModuleNotFoundError: No module named 'torch'`

This route was abandoned in favor of the official Docker image because it was too fragile.

## Docker Desktop / WSL Integration

Actions completed:

- Enabled Docker Desktop WSL integration for the `Ubuntu` distro.
- Fixed Docker socket access inside Ubuntu by adding the user to the `docker` group.
- Verified:

```bash
docker --version
docker ps
```

## Official PaddleX Docker Path

The working direction was to use the official PaddleX vLLM server container instead of installing the whole runtime directly into Ubuntu.

Initial image run:

```bash
docker run -it --rm --gpus all --network host \
  ccr-2vdh3abv-pub.cnc.bj.baidubce.com/paddlepaddle/paddlex-genai-vllm-server \
  paddlex_genai_server --model_name PaddleOCR-VL-0.9B --host 0.0.0.0 --port 8118 --backend vllm
```

What happened:

- Image pulled successfully
- Model downloaded successfully
- vLLM started
- But engine initialization failed because default memory settings were not suitable for a 10 GB RTX 3080

## VRAM / Model Findings

Observed from logs:

- `model.safetensors` size is about `1.92 GB`
- Layout weights were about `212 MB`
- vLLM reported model load size around `1.8236 GiB`
- Free memory at startup was around `8.82 / 10.0 GiB`

Memory-related failures seen:

1. `gpu_memory_utilization=0.9`
   - failed because desired memory was `9.0 GiB`
   - actual free GPU memory was only `8.82 GiB`

2. Default config with too-low utilization
   - failed due to no available KV cache memory

Practical conclusion:

- `gpu-memory-utilization: 0.8` is appropriate for this current machine state
- `0.85` may work
- `0.9` was too high with current free VRAM

## Persistent Model Cache

Earlier runs redownloaded the model because they used `--rm` without a persistent cache mount.

To fix that, a Docker volume was created:

```bash
docker volume create paddlex_cache
```

Then the container was run with:

```bash
-v paddlex_cache:/root/.paddlex
```

That means future runs using the same volume should reuse model files instead of downloading them again.

## Working Container Command So Far

The command that successfully brought the API server up was:

```bash
docker run -it --rm --gpus all --network host \
  -e PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
  -v paddlex_cache:/root/.paddlex \
  ccr-2vdh3abv-pub.cnc.bj.baidubce.com/paddlepaddle/paddlex-genai-vllm-server \
  /bin/bash -lc "printf 'gpu-memory-utilization: 0.8\nmax-num-seqs: 4\n' > /tmp/vllm_config.yaml && paddlex_genai_server --model_name PaddleOCR-VL-0.9B --backend vllm --host 0.0.0.0 --port 8118 --backend_config /tmp/vllm_config.yaml"
```

Important observations from the successful startup logs:

- API server started on `http://0.0.0.0:8118`
- `Application startup complete.`
- vLLM still reported:
  - `max_model_len = 16384`
  - WSL warning: `pin_memory=False`
- Log also reported the exact memory picture:
  - weights about `1.82 GiB`
  - peak activation about `3.92 GiB`
  - CUDAGraph memory about `0.07 GiB`
  - KV cache memory about `2.24 GiB`

## Current State

At the end of this setup:

- WSL2 is installed and working
- Ubuntu is installed and working
- GPU passthrough is working inside WSL2
- Docker Desktop is integrated with Ubuntu
- Official PaddleX vLLM server container can start successfully
- Windows-side config was changed to use `vllm-server`

## Remaining Windows-Side Steps

These are still needed if not already completed after this note:

1. Keep the Ubuntu/Docker terminal running with the active vLLM container.
2. Restart the Windows VLM bridge:
   - `parsing/paddleocr_vl_server.py`
3. Restart the main server if needed:
   - `parsing/n8n_minicpm_server.py --serve`
4. Verify bridge health:
   - `http://127.0.0.1:5006/health`
   - should report backend `vllm-server`
5. Re-test:
   - `POST /?type=purchase&company=K%20V%20ENTERPRISES&check=duplicacy&ocr=vlm`

## Known Remaining Problem

Speed is only one half of the problem.

The parser/extraction layer still has a correctness issue:

- real PaddleOCR-VL output contains HTML-heavy tables
- current code reparses rendered markdown/HTML
- line item extraction can still fail even when VLM output is good

Relevant code:

- [purchase_ocrvl_pipeline.py](/D:/Desktop/TallyBridge/parsing/purchase_ocrvl_pipeline.py:173)
- [purchase_ocrvl_pipeline.py](/D:/Desktop/TallyBridge/parsing/purchase_ocrvl_pipeline.py:204)
- [purchase_ocrvl_pipeline.py](/D:/Desktop/TallyBridge/parsing/purchase_ocrvl_pipeline.py:230)
- [purchase_ocrvl_pipeline.py](/D:/Desktop/TallyBridge/parsing/purchase_ocrvl_pipeline.py:404)

So after backend speed is validated, the next likely engineering task is to fix the extraction layer to use better table/header detection or structured output rather than fragile markdown/HTML reparsing.

## Suggested Next Task For Claude

1. Verify the Windows bridge is actually using `vllm-server`.
2. Benchmark the endpoint again with the new backend.
3. Compare:
   - native timings
   - vLLM timings
4. If latency is acceptable, move to fixing parser correctness.
5. If latency is still poor, tune:
   - backend config YAML
   - cache reuse
   - concurrency
   - model context / memory settings

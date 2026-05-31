# Custom RunPod serverless worker pinned to vLLM 0.9.2 (the version proven to
# serve nanonets/Nanonets-OCR2-3B correctly — 0.15-0.20 hang/crash/garbage).
# Bases on the official vLLM OpenAI image so the vLLM version is guaranteed,
# then adds a small RunPod serverless handler that proxies jobs to the
# in-container vLLM OpenAI server on 127.0.0.1:8000.
FROM vllm/vllm-openai:v0.9.2

RUN pip install --no-cache-dir runpod requests

WORKDIR /app
COPY handler.py /app/handler.py

# Override the base image's vLLM entrypoint; we launch vLLM ourselves from handler.py.
ENTRYPOINT []
CMD ["python3", "/app/handler.py"]

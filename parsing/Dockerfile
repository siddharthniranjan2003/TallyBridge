# Purchase OCR parsing server (the /docstrange?purchase=all&source=runpod endpoint).
# CPU-only: the heavy OCR runs on RunPod (Nanonets); this service rasterizes the
# PDF (Poppler), calls RunPod, runs the per-vendor rule engine + fuzzy match
# against live Supabase stock, and POSTs the voucher to the backend push-queue.
FROM python:3.11-slim

# pdf2image shells out to the Poppler binary (pdftoppm) — it is NOT a pip package.
RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Bind to all interfaces so Render can route to it (default is 127.0.0.1).
ENV MINICPM_HTTP_HOST=0.0.0.0
# Higher-DPI render is unnecessary at 200; 200 DPI gave the best 8-vendor result.
ENV MINICPM_RUNPOD_PDF_DPI=200

# Render injects $PORT at runtime. os.getenv(MINICPM_HTTP_PORT) takes priority
# over the .env file, so this cleanly overrides the local default.
CMD ["sh", "-c", "MINICPM_HTTP_PORT=${PORT:-5003} python n8n_minicpm_server.py --serve"]

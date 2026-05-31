# Best PaddleOCR Setup From Our Tests

## Environment

- Use Python 3.11 for PaddleOCR.
- Working combo we used: `paddlepaddle==3.0.0` and `paddleocr==2.7.3`.

## Input Handling

- If the source is a scanned PDF, keep it as PDF.
- Render PDF pages to images at 300 DPI with PyMuPDF before OCR.
- Avoid converting good scans into lossy JPEG if you can help it.

## Recommended Pipeline

```python
import fitz
from paddleocr import PaddleOCR

def render_pdf_page(pdf_path, dpi=300):
    doc = fitz.open(pdf_path)
    page = doc[0]
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    return pix.tobytes("png")

ocr = PaddleOCR(
    use_angle_cls=True,
    lang="en",
    rec_batch_num=6,
)
```

## Optional Second Pass

```python
ocr_high = PaddleOCR(
    use_angle_cls=True,
    lang="en",
    det_limit_side_len=1920,
    rec_batch_num=6,
)
```

## What Actually Worked Best

- Best overall practical setup: `PDF -> PyMuPDF 300 DPI -> raw PaddleOCR`
- `det_limit_side_len=1920` helped a little on some spacing and character issues.
- But the 1920 setting was not a clean win. It sometimes introduced regressions.
- In one real invoice test, it changed `31-03-2026` to `31-03-2020`.
- So `1920` is useful as a comparison pass, not as the only trusted pass.

## What Did Not Help

- Preprocessing with grayscale + denoise + adaptive threshold + upscale made results worse on our invoice tests.
- docTR was not clearly better than raw PaddleOCR on our sample invoices.

## Accuracy Conclusion

PaddleOCR is good enough for:

- invoice number
- date
- GST/tax totals
- party names
- item codes
- quantities
- rates

PaddleOCR is not good enough by itself for:

- exact verbatim item descriptions
- perfect punctuation and spacing
- fully trusted no-review JSON output

## Practical Rule

- If you run only one pass, use raw PaddleOCR.
- If you can afford two passes, run both:
- raw
- highres (`det_limit_side_len=1920`)
- When they disagree on critical fields like date, GST, totals, PAN, GSTIN, trust raw or flag for review.

## Short Final Recommendation

For best possible results with Paddle:

- Scanned PDF -> render at 300 DPI with PyMuPDF -> raw PaddleOCR
- Use `det_limit_side_len=1920` only as an auxiliary comparison pass.
- Do not rely on heavy preprocessing by default.

---

# PaddleOCR-VL-1.5 Engine (the `ocr=vlm` route)

PaddleOCR-VL is a 0.9B document-parsing vision-language model. It emits structured
markdown (invoice tables become markdown tables), which is more robust than the
classic pixel-column heuristics. It powers `POST /?type=purchase&ocr=vlm`.

## Why a separate venv

PaddleOCR-VL needs `paddleocr>=3.2` (`[doc-parser]`), which conflicts with the
classic runner's `paddleocr==2.7.3`. Install it in its own venv.

## Install (Windows, NVIDIA GPU)

```
py -3.11 -m venv parsing\.venv-ocrvl
parsing\.venv-ocrvl\Scripts\pip install paddlepaddle-gpu
parsing\.venv-ocrvl\Scripts\pip install "paddleocr[doc-parser]"
```

The first `predict()` auto-downloads the PaddleOCR-VL-1.5 and PP-DocLayoutV2 weights.
On native Windows use `MINICPM_VLM_BACKEND=native` (vLLM has no native Windows build).

## Optional: vLLM acceleration (Linux / WSL2 only)

In a separate venv:

```
paddleocr genai_server --model_name PaddleOCR-VL-1.5-0.9B --backend vllm --port 8118
```

Then set `MINICPM_VLM_BACKEND=vllm-server` and `MINICPM_VLM_VLLM_URL=http://127.0.0.1:8118/v1`.

## Run

```
parsing\.venv-ocrvl\Scripts\python parsing\paddleocr_vl_server.py   # port 5006, model stays warm
python parsing\n8n_minicpm_server.py --serve                        # port 5003
```

The n8n server (port 5003) calls the VLM server (port 5006) over HTTP per request.
Relevant `.env` keys: `MINICPM_VLM_SERVER_URL`, `MINICPM_VLM_HTTP_PORT`,
`MINICPM_VLM_BACKEND`, `MINICPM_VLM_VLLM_URL`, `MINICPM_VLM_REQUEST_TIMEOUT`.

## Request

```
POST http://127.0.0.1:5003/?type=purchase&company=K%20V%20ENTERPRISES&check=duplicacy&ocr=vlm
```

`ocr` defaults to `paddle` (classic engine, unchanged). `ocr=vlm` selects this model.

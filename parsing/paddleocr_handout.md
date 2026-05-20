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

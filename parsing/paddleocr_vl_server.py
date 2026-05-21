"""
Persistent local PaddleOCR-VL-1.5 server.

Loads the PaddleOCR-VL document-parsing pipeline once and keeps it warm, exposing
a tiny HTTP API the main n8n server calls for the `ocr=vlm` purchase route.

Run (in the dedicated PaddleOCR-VL venv):
    python paddleocr_vl_server.py

Endpoints:
    GET  /health  -> readiness + backend info
    POST /parse   -> accepts an invoice image/PDF, returns parsed markdown + layout

See paddleocr_handout.md for install steps.
"""
from __future__ import annotations

import base64
import json
import sys
import tempfile
import time
import traceback
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from lib.env import make_env_loader

_env = make_env_loader(SCRIPT_DIR / ".env")


def _parse_optional_bool(raw: str | None) -> bool | None:
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    return value.lower() not in {"0", "false", "no", "off"}


def _parse_optional_list(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    parts = [item.strip() for item in str(raw).split(",")]
    parts = [item for item in parts if item]
    return parts or None

SERVE_HOST = _env("MINICPM_VLM_HTTP_HOST", "127.0.0.1")
SERVE_PORT = int(_env("MINICPM_VLM_HTTP_PORT", "5006") or "5006")
MAX_UPLOAD_BYTES = int(_env("MINICPM_VLM_MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))
# native  -> PaddleOCR-VL runs in-process via paddlepaddle(-gpu)
# vllm-server -> recognition is delegated to a `paddleocr genai_server` (Linux/WSL2)
BACKEND = (_env("MINICPM_VLM_BACKEND", "native") or "native").strip().lower()
VLLM_URL = _env("MINICPM_VLM_VLLM_URL", "http://127.0.0.1:8118/v1")
PIPELINE_VERSION = _env("MINICPM_VLM_PIPELINE_VERSION", "")
VLLM_MODEL_NAME = _env("MINICPM_VLM_API_MODEL_NAME", "PaddleOCR-VL-0.9B")
NATIVE_MODEL_LABEL = "PaddleOCR-VL-1.5"
DOC_PREPROCESS = _parse_optional_bool(_env("MINICPM_VLM_USE_DOC_PREPROCESSOR", ""))
DOC_ORIENTATION = _parse_optional_bool(_env("MINICPM_VLM_USE_DOC_ORIENTATION_CLASSIFY", ""))
DOC_UNWARPING = _parse_optional_bool(_env("MINICPM_VLM_USE_DOC_UNWARPING", ""))
USE_LAYOUT_DETECTION = _parse_optional_bool(_env("MINICPM_VLM_USE_LAYOUT_DETECTION", ""))
FORMAT_BLOCK_CONTENT = _parse_optional_bool(_env("MINICPM_VLM_FORMAT_BLOCK_CONTENT", ""))
MERGE_LAYOUT_BLOCKS = _parse_optional_bool(_env("MINICPM_VLM_MERGE_LAYOUT_BLOCKS", ""))
MARKDOWN_IGNORE_LABELS = _parse_optional_list(_env("MINICPM_VLM_MARKDOWN_IGNORE_LABELS", ""))
PROMPT_LABEL = (_env("MINICPM_VLM_PROMPT_LABEL", "") or "").strip() or None

_PIPELINE = None


def active_model_label() -> str:
    if BACKEND == "vllm-server":
        return VLLM_MODEL_NAME
    return NATIVE_MODEL_LABEL


def build_pipeline():
    """Instantiate the PaddleOCR-VL pipeline once; raises if paddleocr is missing."""
    from paddleocr import PaddleOCRVL

    kwargs: dict = {}
    if PIPELINE_VERSION:
        kwargs["pipeline_version"] = PIPELINE_VERSION
    effective_doc_orientation = DOC_ORIENTATION
    effective_doc_unwarping = DOC_UNWARPING
    if DOC_PREPROCESS is not None:
        if effective_doc_orientation is None:
            effective_doc_orientation = DOC_PREPROCESS
        if effective_doc_unwarping is None:
            effective_doc_unwarping = DOC_PREPROCESS
    if effective_doc_orientation is not None:
        kwargs["use_doc_orientation_classify"] = effective_doc_orientation
    if effective_doc_unwarping is not None:
        kwargs["use_doc_unwarping"] = effective_doc_unwarping
    if USE_LAYOUT_DETECTION is not None:
        kwargs["use_layout_detection"] = USE_LAYOUT_DETECTION
    if FORMAT_BLOCK_CONTENT is not None:
        kwargs["format_block_content"] = FORMAT_BLOCK_CONTENT
    if MERGE_LAYOUT_BLOCKS is not None:
        kwargs["merge_layout_blocks"] = MERGE_LAYOUT_BLOCKS
    if MARKDOWN_IGNORE_LABELS is not None:
        kwargs["markdown_ignore_labels"] = MARKDOWN_IGNORE_LABELS
    if BACKEND == "vllm-server":
        # The WSL vLLM server is currently serving PaddleOCR-VL-0.9B, so pass
        # the exact served model name to the client wrapper instead of the
        # native PaddleOCR-VL-1.5 default.
        kwargs["vl_rec_backend"] = "vllm-server"
        kwargs["vl_rec_server_url"] = VLLM_URL
        kwargs["vl_rec_api_model_name"] = VLLM_MODEL_NAME
    return PaddleOCRVL(**kwargs)


def _markdown_of(result) -> str:
    """Best-effort extraction of page markdown across paddleocr result shapes."""
    markdown = getattr(result, "markdown", None)
    if isinstance(markdown, dict):
        texts = markdown.get("markdown_texts")
        if isinstance(texts, list):
            return "\n\n".join(str(part) for part in texts if part)
        if texts:
            return str(texts)
    if isinstance(markdown, str):
        return markdown
    return ""


def _layout_of(result) -> dict | list:
    """Best-effort, JSON-safe extraction of the layout result."""
    payload = getattr(result, "json", None)
    if payload is None:
        return {}
    if isinstance(payload, dict):
        payload = payload.get("res", payload)
    try:
        json.dumps(payload)
        return payload
    except (TypeError, ValueError):
        return {}


def parse_document(file_path: Path) -> dict:
    if _PIPELINE is None:
        raise RuntimeError("PaddleOCR-VL pipeline is not loaded.")
    started = time.time()
    predict_kwargs: dict = {}
    if PROMPT_LABEL:
        predict_kwargs["prompt_label"] = PROMPT_LABEL
    results = list(_PIPELINE.predict(str(file_path), **predict_kwargs))
    pages = [_markdown_of(res) for res in results]
    layout = [_layout_of(res) for res in results]
    return {
        "ok": True,
        "backend": BACKEND,
        "page_count": len(results),
        "markdown": "\n\n".join(page for page in pages if page),
        "page_markdown": pages,
        "layout": layout,
        "inference_seconds": round(time.time() - started, 2),
    }


def _decode_upload(body: bytes, content_type: str) -> tuple[bytes, str]:
    lowered = (content_type or "").lower()
    if lowered.startswith("application/json"):
        payload = json.loads(body.decode("utf-8"))
        file_b64 = payload.get("image_base64") or payload.get("file_base64") or ""
        if not file_b64:
            raise ValueError("JSON body must include image_base64 or file_base64")
        return base64.b64decode(file_b64), payload.get("filename", "upload.jpg")
    if lowered.startswith("multipart/form-data"):
        message = BytesParser(policy=default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
        )
        for part in message.iter_parts():
            if "form-data" not in part.get("Content-Disposition", ""):
                continue
            content = part.get_payload(decode=True) or b""
            if content:
                return content, part.get_filename() or "upload.jpg"
        raise ValueError("Multipart request did not include a file")
    return body, "upload.bin"


def _suffix_for(content: bytes, filename: str) -> str:
    if content.startswith(b"%PDF-"):
        return ".pdf"
    suffix = Path(filename or "").suffix.lower()
    if suffix in {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}:
        return suffix
    return ".jpg"


class PaddleVLHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.rstrip("/") in {"", "/health"}:
            self._send_json(200, {
                "ok": _PIPELINE is not None,
                "status": "ready" if _PIPELINE is not None else "loading",
                "service": "paddleocr_vl_server",
                "model": active_model_label(),
                "backend": BACKEND,
                "port": SERVE_PORT,
                "pipeline_version": PIPELINE_VERSION or "default",
                "config": {
                    "use_doc_preprocessor": (
                        DOC_PREPROCESS
                        if DOC_PREPROCESS is not None
                        else bool(DOC_ORIENTATION) or bool(DOC_UNWARPING)
                    ),
                    "use_doc_orientation_classify": DOC_ORIENTATION,
                    "use_doc_unwarping": DOC_UNWARPING,
                    "use_layout_detection": USE_LAYOUT_DETECTION,
                    "format_block_content": FORMAT_BLOCK_CONTENT,
                    "merge_layout_blocks": MERGE_LAYOUT_BLOCKS,
                    "markdown_ignore_labels": MARKDOWN_IGNORE_LABELS,
                    "prompt_label": PROMPT_LABEL,
                },
            })
            return
        self._send_json(404, {"ok": False, "error": "Not found"})

    def do_POST(self):
        if self.path.rstrip("/") not in {"/parse"}:
            self._send_json(404, {"ok": False, "error": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_UPLOAD_BYTES:
                self._send_json(400, {"ok": False, "error": "Bad request size"})
                return
            body = self.rfile.read(length)
            content, filename = _decode_upload(body, self.headers.get("Content-Type", ""))
            with tempfile.NamedTemporaryFile(suffix=_suffix_for(content, filename), delete=False) as handle:
                handle.write(content)
                temp_path = Path(handle.name)
            try:
                payload = parse_document(temp_path)
            finally:
                temp_path.unlink(missing_ok=True)
            self._send_json(200, payload)
        except Exception as exc:
            self._send_json(
                400 if isinstance(exc, ValueError) else 500,
                {"ok": False, "status": "error", "error": str(exc), "traceback": traceback.format_exc()},
            )

    def _send_json(self, status_code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def main() -> int:
    global _PIPELINE
    print(f"Loading {active_model_label()} (backend={BACKEND})...")
    _PIPELINE = build_pipeline()
    print(f"PaddleOCR-VL server ready on http://{SERVE_HOST}:{SERVE_PORT}")
    try:
        ThreadingHTTPServer((SERVE_HOST, SERVE_PORT), PaddleVLHandler).serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

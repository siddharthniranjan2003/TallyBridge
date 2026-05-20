#!/usr/bin/env python3
"""
Standalone tester for the PaddleOCR-VL markdown parser.

Runs ONLY the pure-Python parsing path from purchase_ocrvl_pipeline.py — the
markdown table extraction, _COLUMN_KEYWORDS matching, table selection, row
extraction and header-field regexes. It does NOT boot the GPU model or any HTTP
server, so iterating on _COLUMN_KEYWORDS / table-selection tuning is instant.

Usage:
    python purchase/test_ocrvl_parser.py [path/to/raw_markdown.md]

With no argument it uses the bundled sample_ocrvl_invoice.md. To test against a
real document, copy the `ocr.raw_markdown` field out of a
/?type=purchase&ocr=vlm response, save it to a .md file, and pass that path.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Make the top-level parsing/ modules importable when run from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from purchase_ocrvl_pipeline import (  # noqa: E402
    extract_markdown_tables,
    parse_vlm_invoice,
    select_item_table,
)

SAMPLE = Path(__file__).resolve().parent / "sample_ocrvl_invoice.md"


def main(argv: list[str]) -> int:
    md_path = Path(argv[1]) if len(argv) > 1 else SAMPLE
    if not md_path.exists():
        print(f"error: markdown file not found: {md_path}", file=sys.stderr)
        return 1
    if len(argv) <= 1:
        print(f"# no path given - using bundled sample: {md_path}", file=sys.stderr)

    markdown = md_path.read_text(encoding="utf-8")

    tables = extract_markdown_tables(markdown)
    selected, mapping = select_item_table(tables)
    header, description_rows, numeric_rows, warnings = parse_vlm_invoice(markdown)

    report = {
        "source": str(md_path),
        "diagnostics": {
            "tables_detected": len(tables),
            "all_table_headers": [table[0] for table in tables],
            "selected_table_header": selected[0] if selected else [],
            "selected_table_data_rows": max(0, len(selected) - 1),
            "column_mapping": mapping,
        },
        "header": header,
        "line_items": [
            {**desc, **num} for desc, num in zip(description_rows, numeric_rows)
        ],
        "row_count": len(description_rows),
        "warnings": warnings,
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

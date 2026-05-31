# Purchase invoice pipeline (RunPod Nanonets → Tally → Supabase push_queue)

This package turns a scanned/native purchase-invoice PDF into a Tally voucher and
enqueues it to the cloud `push_queue`. The flow is **vendor-specific by design**:
every vendor prints a consistent layout, so each stage keys off the detected vendor.

## End-to-end flow

```
invoice PDF
  │
  ▼  server/handler.py :: call_runpod_markdown_ocr
RunPod / Nanonets-OCR2-3B  (each PDF page OCR'd separately at full resolution,
  │                          markdown concatenated with "---" separators)
  ▼  markdown
purchase_docstrange_pipeline.py :: run_docstrange_purchase_all_pipeline
  │
  ├─▶ purchase/vlm_vendor_parser.py :: parse_vlm_invoice
  │      • build_ocr_lines        – flatten markdown + HTML/markdown tables to lines
  │      • _extract_vendor_name   – read supplier name from the header
  │      • detect_vendor          – map name → vendor code  (purchase/vendor.py)
  │      • _parse_vendor_items    – VENDOR-SPECIFIC row parser → description+numeric rows
  │      • header extraction      – invoice number, date, tax entries
  │
  ├─▶ purchase/voucher_builder.py :: combine_ocr_items → PurchaseRawItem[]
  │
  ├─▶ conversion algorithm (VENDOR-SPECIFIC), per raw item:
  │      • build_candidate_queries  – vendor rules turn raw OCR text into stock queries
  │      • candidate_group_filter / candidate_vendor_tokens / family_filter – narrow CSV rows
  │      • similarity_score + match_preference – pick best stock_item_name
  │      reference data: D:\Desktop\excels\<vendor>.csv  (VENDOR_REFERENCE_CSV)
  │
  ▼  voucher_payload  (+ source_payload with score/source per item)
push_queue_request_payload
  │
  ▼  server/handler.py :: post_to_push_queue  →  POST /api/sync/push-queue (backend :3001)
Supabase push_queue   (status = pending)
```

## Per-vendor components

| Vendor PDF | Code | Detect keyword (vendor.py) | Row parser (vlm_vendor_parser.py) | Query builder + repair (voucher_builder.py) | Reference CSV |
|---|---|---|---|---|---|
| addison      | ADDISON  | ADDISON                    | `_parse_addison_items`  | `build_candidate_queries`/`repair_addison_description` | ADDISON.csv |
| cp           | CP       | CP GRAT-EX / GRAT-EX       | `_parse_cp_items`       | CP branch / `repair_cp_description`         | cp greatex.csv |
| emkay        | ET       | EMKAY                      | `_parse_item_lines`     | ET branch / `repair_et_description`         | ET16Final.csv |
| saint global | GNL      | GRINDWELL / NORTON         | `_parse_gnl_items`      | GNL branch / `repair_gnl_description`       | saint global.csv |
| pidilite     | PIDILITE | PIDIL* / STEELGRIP         | `_parse_pidilite_items` | PIDILITE branch / `repair_pidilite_description` | pidilite.csv |
| rr           | RR       | R R TOOLS                  | `_parse_rr_items`       | RR branch / `repair_rr_description`         | rrtools.csv |
| totem        | TOTEM    | FORBES / TOTEM             | `_parse_totem_items`    | TOTEM branch / `repair_totem_description`   | FORBES.csv |
| wikus        | WIKUS    | WIKUS                      | `_parse_wikus_items`    | WIKUS branch / `repair_wikus_description`   | wikus.csv |
| (stanley)    | STANLEY  | STANLEY / LENOX            | `_parse_stanley_items`  | STANLEY branch / `repair_stanley_description` | — |

## DocStrange/Nanonets-tuned parsing

Nanonets emits the item table as an HTML `<table>` or a markdown pipe-table, which
`build_ocr_lines` flattens into one line per row. Several vendors need a DocStrange
row regex distinct from the original PaddleOCR layout:
`GNL_DOCSTRANGE_ROW_RE`, `STANLEY_DOCSTRANGE_ROW_RE`, `PIDILITE_DOCSTRANGE_ROW_RE`,
`WIKUS_DOCSTRANGE_ROW_RE`, and the TOTEM row regex.

## Inspecting a run

`outputs/vendors/<VENDOR>/` holds the readable flow artifacts for the latest test run:

```
1_ocr.md            raw Nanonets markdown
2_parsed.json       vendor-specific parsed JSON (header + description/numeric rows)
3_voucher.json      Tally voucher payload after the conversion algorithm
4_queue_request.json  exact body POSTed to /api/sync/push-queue
5_queue_response.json backend response incl. Supabase job id + status
```

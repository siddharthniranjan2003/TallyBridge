# RAG Discussion — testslm Challan Pipeline

## What is RAG (Simple to Detailed)

**Simplest:** Instead of asking the AI to remember everything, you look up relevant facts first, then hand them to the AI along with the question.

**Plain English:** Right now MiniCPM reads a challan image and guesses item names from scratch. RAG means: before asking MiniCPM, you search your stock list for the closest items and say "here are 10 likely candidates — pick the right one." The model doesn't have to guess blindly.

**Technical:** An embedding model maps any text to a fixed-size vector in high-dimensional space where semantically similar texts are geometrically close. You precompute and store these vectors in pgvector (a Postgres extension Supabase already supports). At query time, a cosine similarity search returns the nearest neighbors in ~30ms regardless of how many rows exist — far more robust than O(N) fuzzy string scan.

---

## How RAG Differs from Fuzzy Matching

**Fuzzy matching** measures character/word overlap:
- "SOL CARBIDE ENDMLL 8 ADD" vs "SOLID CARBIDE ENDMILL 8 ADDISON" → rapidfuzz sees missing letters, scores ~71% → might miss it

**Vector search** measures meaning similarity:
- Both strings embed to nearly identical vectors → match found regardless of spelling differences

The embedding model was trained on millions of texts so it knows `SOL` and `SOLID` are the same concept. Fuzzy doesn't — it just counts character edits.

**They are complementary, not alternatives.** RAG narrows 5000 items → 15. Fuzzy picks 1 from 15.

---

## What is a Vector

A list of numbers representing meaning. Like GPS coordinates — except instead of 2 numbers placing you on a map, 768-1024 numbers place a phrase in "meaning space." Items that mean similar things land near each other. You search by distance, not by spelling.

---

## Where RAG Fits in the Current Workflow

```
Image → MiniCPM OCR → raw text → [RAG here] → matched stock item
```

RAG is **after** OCR, not before. The one exception is party-name context injection (optional):
- Once party is identified from header crop, retrieve their past items from Supabase
- Inject as hints into the OCR prompt → helps MiniCPM resolve ambiguous sizes

---

## When Embedding Happens

| Phase | When | Frequency |
|---|---|---|
| Stock indexing | One-time setup | Once; re-run only when stock changes |
| Query embedding | Per challan, per item (~15 items) | Every request, ~5-10ms per item |

---

## When MiniCPM Runs (Relevant to VRAM Planning)

MiniCPM runs **twice per challan**, sequentially:
1. `query_ollama_chat(image_b64)` — full image, reads all item rows (~20-60s)
2. `query_ollama_text(cropped_header, PARTY_NAME_PROMPT)` — party name only (~5-10s)

Embedding runs **after both MiniCPM calls finish** — they never overlap. This means the full 10GB VRAM is available for the embedding model when it runs.

---

## Hardware (RTX 3080 10GB, 64GB RAM, i9-10900K)

Since MiniCPM and embedding are sequential, full VRAM is available for embedding. No need to compromise on model size.

---

## Embedding Model Benchmarks (MTEB Retrieval Score)

| Rank | Model | MTEB Retrieval | Dimensions | VRAM | Ollama |
|---|---|---|---|---|---|
| 1 | **qwen3-embedding:8b** | **70.58** | 1024 | ~3.7GB | ✅ |
| 2 | bge-m3 | 63.0 | 1024 | ~1.2GB | ✅ |
| 3 | snowflake-arctic-embed:l | 55.98 | 1024 | ~0.8GB | ✅ |
| 4 | mxbai-embed-large | 54.39 | 1024 | ~1.3GB | ✅ |
| 5 | bge-large-en-v1.5 | 54.29 | 1024 | ~1.3GB | ✅ |
| 6 | nomic-embed-text | 49.01 | 768 | ~0.5GB | ✅ |

**Note:** `mxbai-embed-large` has a 512-token context limit — would truncate longer item descriptions.

---

## Chosen Model: `qwen3-embedding:8b`

```bash
ollama pull qwen3-embedding:8b
```

- 16 points above mxbai on retrieval benchmarks
- 131k token context — no truncation ever
- Fits comfortably in 10GB VRAM when MiniCPM is idle
- Strong on short technical phrases with abbreviations, sizes, and brand names

Fallback if lighter model needed: `bge-m3` (63.0 score, only 1.2GB)

---

## Planned Implementation

- Add `embedding vector(1024)` column to Supabase `stock_items` table (pgvector)
- One-time script: embed all stock items from CSV/table → store in pgvector
- Per-request: embed each OCR'd item → vector search top-15 → rapidfuzz picks final match from those 15
- Optional: party-history context injection into OCR prompt after party name is identified

---

## Not Yet Implemented
RAG is discussed and planned but not yet coded into `test_minicpm.py` or `n8n_minicpm_server.py`.

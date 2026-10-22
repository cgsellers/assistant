# Walkthrough: one receipt, start to finish

`DESIGN.md` records *why* each decision was made, ordered by decision. This document
is the other view: *how the pieces connect*, ordered by what happens to a single
receipt. Every step names the file and function that does it, and links to the
design section that explains the choice.

Read this first, then dip into `DESIGN.md` for whichever step you want to defend.

---

## The map

```
             ┌────────────── API (api/) ──────────────┐
  upload ──> │ require_api_key → upload → ingest      │
             └───────────────────┬────────────────────┘
                                 │ documents row + jobs row (one transaction)
                                 ▼
             ┌──────────── worker (worker/run.py) ───────────────────────────┐
             │ claim ─> run_ocr_job ──────> ocr_results + queues "extract"   │
             │ claim ─> run_extract_job:                                     │
             │            route ─> build_json_schema ─> extract ─> validate  │
             │            ─> record_extraction ─> to_ledger ─> transaction   │
             └──────────────────────────────┬────────────────────────────────┘
                                            ▼
                         transactions (the ledger)
                          ▲                      ▲
         analyst/ (questions)            evals/ (accuracy, cost)
```

Three layers of data, from least to most trusted to act on:

| Layer | Tables | What it holds |
|---|---|---|
| Raw | `documents`, `ocr_results` | What was uploaded and what OCR read. Never edited |
| Typed | `extractions.raw_json` | What the model said, shaped by a document type |
| Canonical | `transactions` | The narrow ledger. The only thing the analyst reads |

---

## 1. The upload arrives

`POST /documents` with a file and `Authorization: Bearer lk_...`.

**Auth runs before anything else.** `core/auth.py: require_api_key` is attached to the
whole app in `api/main.py`, not to a router, so a route added later is protected by
default. It hashes the presented key and looks the hash up in `api_keys`. Absent,
unknown and revoked keys all get the same 401. `/health` and the docs are the only
public paths. → §5.22

**Then ingestion.** `api/routes/documents.py: upload` reads the bytes, checks size,
and sniffs the real type from magic bytes rather than trusting the `Content-Type`
header (§5.5). It hands off to `core/repository.py: ingest`, which:

1. hashes the bytes (`core/storage.py: sha256_hex`)
2. returns the existing document if that hash has been seen. **This is dedup**: same
   bytes, same id, no new work, `duplicate: true` in the response (§5.7)
3. writes the file **before** the row, to `raw/<2 hex>/<sha256>` (§5.4, §5.6). A file
   with no row is harmless garbage. A row with no file breaks the worker
4. inserts the `documents` row, a `document_events` row (`→ uploaded`), and a `jobs`
   row of kind `ocr`, **all in one transaction**, so a document never exists without
   the work to process it

The response comes back immediately. Nothing slow has happened yet.

**Status: `uploaded`**

---

## 2. The worker picks it up

`worker/run.py: main` loops on `tick`. There's no Redis or broker: the `jobs` table
is the queue (§5.3).

**Claiming is one UPDATE.** `claim` flips the oldest ready job from `queued` to
`running` in a single statement. Whichever worker's UPDATE lands first owns the job.
There is no SELECT-then-UPDATE race, because nothing happens between deciding and
taking (§5.13).

**Dead workers don't strand jobs.** `reclaim_stale` returns jobs locked for more than
15 minutes to the queue. It doesn't reset `attempts`, so a job that keeps killing its
worker still runs out of retries instead of looping forever (§5.15).

---

## 3. OCR

`run_ocr_job` loads the bytes and calls whichever `OcrEngine` is configured. Every
engine is wrapped in `ocr/pdftext.py: TextLayerFirst`: a born-digital PDF is read
from its own embedded text, exactly and in milliseconds, and only images and scans
reach OCR (DESIGN §7.11). PP-OCR
runs in its own container. Tesseract runs in-process and is mainly the test double
(§5.12). Both satisfy the same protocol in `ocr/base.py`, which is why the test suite
needs no container.

- **Success:** writes `ocr_results` (text, bounding boxes, confidence), moves the
  document to `ocr_done`, and queues a **second** job of kind `extract` in the same
  transaction.
- **Unreadable bytes:** a permanent failure. Status becomes `ocr_failed`, with no
  retry.
- **Engine errors:** transient. The job retries with exponential backoff (4s, 16s,
  64s) until `max_attempts`.

**Why extraction is a separate job.** OCR is the expensive half. If a model call
fails, the document shouldn't lose its recognised text and pay for OCR again
(`core/status.py: JobKind`).

**Status: `ocr_done`**

---

## 4. Routing: which kind of document is this?

`run_extract_job` rebuilds the text from the stored bounding boxes with
`ocr/layout.py: extraction_text`, so each product and its price sit on one line as
printed. Flattened OCR order had paired names with the wrong prices on two-column
receipts (DESIGN §7.8). Then it calls `doctypes/registry.py: route`.

A document type isn't Python code. It's a **row** in `document_types`, a validated
`Definition` (`doctypes/definition.py`) loaded from YAML. The shipped one is
`doctypes/seeds/receipt.yaml` (§5.20).

`route` scores every active type with `doctypes/schema.py: match_score`, which is
keyword and regex matching with no model call, so it costs nothing. The highest
scorer above its own `min_score` wins.

- **Nothing matches:** status `unsupported`. The model is **never called**, so no
  money is spent on a document nobody knows how to read.
- **A match:** `select_profile` checks whether this issuer has description overrides,
  matching on tax id first, then name.

**Status: `classified`**

---

## 5. Extraction: the only model call in the pipeline

`doctypes/schema.py: build_json_schema` turns the definition into a JSON Schema. The
**field descriptions are the prompt**: "The amount actually charged, labelled TOTAL.
Not SUBTOTAL…". That's why a profile overrides descriptions and never fields.

`llm/anthropic.py: AnthropicExtractor.extract` sends the OCR text with
`output_config.format`, so the API constrains the response to that schema
(structured outputs, not a forced tool call; §5.16). Haiku 4.5 by default, with no
extended thinking, because the answer is on the page.

Every call returns tokens, cost and latency as **fields on the result**, not log
lines. Errors are classified:

- `ExtractionRefused` covers 4xx responses, a refusal, or truncation at `max_tokens`.
  These are permanent; retrying only spends money.
- A refusal or truncation is still **billed**, since it's an HTTP 200 with usage.
  The error carries that spend, and the worker records it as a failed attempt with
  `is_current=False`, so it shows up in the cost report and never replaces a real
  answer (DESIGN §7.9).
- `ExtractionError` covers rate limits, 5xx responses, connection failures, and any
  other SDK error. These are transient, so the job retries.

**Status: `extracted`**

---

## 6. Validation: deterministic rules

`doctypes/validate.py: validate` runs in two steps:

1. **`coerce`** turns the model's strings into real types. Money becomes `Decimal`,
   never `float`. `"1,234"` is **rejected**, not read as 1.234, because a thousands
   separator mistaken for a decimal is a three-orders-of-magnitude error.
2. **`evaluate`** runs the type's rules (`required`, `positive`, `sum_equals`,
   `not_future`, …). A rule whose field came back null is skipped; only `required`
   cares about absence.

The model's raw answer, never the coerced one, is stored by
`core/repository.py: record_extraction` in `extractions`. That table is append-only
with `is_current`, so a retry adds a row rather than overwriting.

- **Any rule fails:** `needs_review`. The answer is kept, because a reviewer needs
  something to correct.
- **All pass:** `validated`.

**Status: `validated`** (or `needs_review`)

---

## 7. The ledger

`doctypes/canonical.py: to_ledger` reads the type's `canonical:` block, a mapping
written in YAML rather than Python (§5.21):

```yaml
canonical:
  direction: out
  amount: total
  occurred_on: issued_on
  counterparty: vendor
```

`core/repository.py: record_transaction` writes one row to `transactions` and
demotes the previous row for that document. **There is one live row per document**,
so re-extracting a receipt never counts it twice.

Nothing is inferred on the way in. A receipt with no printed currency gets a null
currency, not EUR because it looks Spanish. Amounts pass through the `Money` column
type (`core/models.py`), so they read back in one canonical spelling. → `core/money.py`

**Status: `committed`**

`document_events` now holds the whole history:

```
uploaded → ocr_done → classified → extracted → validated → committed
```

---

## 8. Asking questions about it

`analyst/agent.py: Analyst.ask` is a hand-written tool-use loop over six read-only
tools in `analyst/tools.py`.

- **The model never does arithmetic.** It picks a tool such as `total_spend`, the sum
  happens in SQL, and the model explains the result.
- **Totals never cross currencies.** EUR and USD come back as separate lines.
- **Citations come from tool results**, through the typed `ToolResult.document_ids`,
  never from the model's prose. So a document id the model invents can't reach
  `Answer.sources`.
- **It can't write.** None of the tools write, so "requires approval" holds because
  the capability doesn't exist, not because the model obeys a rule.

→ DESIGN §7.7

---

## 9. Measuring whether any of it is right

`evals/` is separate from the pipeline and never writes to it.

| Command | What it does |
|---|---|
| `python -m evals.run --draft-labels` | Runs extraction on unlabelled receipts and writes `verified: false` label files for you to correct |
| `python -m evals.run --dry-run` | Validates labels without calling the model |
| `python -m evals.run` | Scores per field: correct, missed, **hallucinated**, wrong |
| `python -m evals.run --compare claude-sonnet-5` | Scores two models on the same corpus and lists regressions first |
| `python -m core.costs` | What extraction has cost: total, wasted, per document, by model |

The runner refuses unverified labels; otherwise the model would be grading its own
homework. The real corpus lives in `data/evals/`, which is gitignored because real
receipts carry real tax ids. → DESIGN §7.6

---

## Things that commonly confuse

- **"Typed layer" means JSON.** There are no `receipts` or `line_items` tables.
  `extractions.raw_json` is the typed layer, and only the fields in `canonical:` get
  promoted into real columns (§5.20).
- **`validated` isn't the end state.** A type without a `canonical:` block stops there
  on purpose: it isn't ready for the ledger yet.
- **The model ID in `extractions.model_used` is dated** (`claude-haiku-4-5-20251001`)
  even though the request used the alias. `llm/anthropic.py: pricing_key` strips the
  date suffix so stored rows can still be priced.
- **Two job rows per document is normal**: `ocr` and `extract`.

---

## Where to start reading code

If you have an hour, read in this order. Each one is short and states its reasoning
at the top.

1. `core/repository.py: ingest`: dedup and the one-transaction rule
2. `worker/run.py: claim`, then `run_extract_job`: the whole pipeline in one function
3. `doctypes/seeds/receipt.yaml`: what a document type actually is
4. `doctypes/validate.py`: where money becomes `Decimal`, and the `1,234` trap
5. `analyst/agent.py`: the system prompt and the loop

## Running it yourself

```bash
uv sync
uv run alembic upgrade head
uv run python -m doctypes.seed
uv run python -m core.auth create "me"        # prints a key once
uv run uvicorn api.main:app --reload          # docs at /docs
uv run python -m worker.run                   # in another terminal
uv run pytest                                 # ~4s, no network needed
```

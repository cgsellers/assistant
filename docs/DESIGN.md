# Ledgerline — Technical Design & Decision Record

**Status:** phase 1, ~two-thirds complete · **Last updated:** 2026-09-17 · **Head:** `b014758`

This document records what the system is, how it is built, what has actually been
verified, and *why* each significant choice was made — including the ones that were
reversed. It is the companion to [`../README.md`](../README.md), which is the public
pitch; this is the engineering record.

Where a claim is measured, it says so. Where it is an assumption, it says that too.

---

## 1. What this is

A document ingestion pipeline for freelancer and small-business finances.

A user uploads a receipt or invoice. Local OCR reads it. Deterministic pattern routing
picks a document type. A cheap text model extracts JSON against a strict per-type
Pydantic schema. Deterministic validators check it. Code maps the result into typed
tables and a canonical `transactions` ledger.

That ledger then has consumers: a read-only analyst agent with citations, third-party
integrations via a transactional outbox, exports, and a human review queue.

### Target user

Freelancers and small businesses. That choice drives concrete requirements: VAT and tax
IDs, invoice due dates, multi-currency, and a first integration that is CSV export
followed by an accounting API.

### Why it exists

Most "AI finance assistant" projects put a model in the loop for every step and cannot
answer two basic questions: *how accurate is it?* and *what does one document cost to
process?* This project is built to answer both.

---

## 2. Design principles

These are the load-bearing commitments. Everything in section 5 follows from them.

**1. Models only where perception or judgment is required.**
Model calls happen in exactly three places — classifier, extractor, vision fallback —
and two of those are fallbacks. Everything else is code that can be unit-tested and
cannot hallucinate. OCR, routing, validation, and mapping are all deterministic.

**2. Measure before optimising.**
The eval harness (phase 3) is built *before* the orchestration layer (phase 5),
specifically so the README can show before/after numbers rather than assertions.

**3. Cheap tier first, escalate on failure.**
A cheap text model extracts. If deterministic validation fails, escalate to vision. If
that fails, escalate to a human. Every attempt records `model_used` and `cost_usd`, so
"% of documents handled per tier" is a reportable number.

**4. The analyst never computes.**
Arithmetic lives in deterministic tools over the canonical ledger. The model interprets
and explains; it does not add up. Anything that writes state requires human approval.

**5. Every stage is idempotent.**
A crashed worker retries without duplicating anything. This is enforced structurally
(content-addressed storage, unique constraints, transactional claims) rather than by
convention.

---

## 3. Architecture

```
POST /documents
    │  sha256 dedup, magic-byte type detection, size cap
    ├──> content-addressed file store   (raw/<2 hex>/<sha256>)
    └──> documents row + document_events row + jobs row   [one transaction]
                              │
                    worker polls jobs
                              │
                    OcrEngine.read()  ──> ocr_results (text, bboxes, confidences)
                              │
                    [phase 2] router ──> registry ──> extractor ──> validator ──> mapper
                              │
                    typed tables ──> transactions (canonical)
                              │
                    analyst agent · integrations · exports · review queue
```

### Component boundaries

| Boundary | Why it exists |
|---|---|
| `OcrEngine` protocol | Engines are swappable and, critically, *comparable* — phase 3 runs several over one labeled set |
| `ChatProvider` protocol (phase 2) | Model calls stream typed events, not strings; keeps provider choice out of business logic |
| Document type registry (phase 2) | Adding a document type means adding one module; nothing else in the pipeline changes |
| Three data layers | Raw is immutable evidence, typed is per-document-type, canonical is the single thing the analyst reads |

---

## 4. Data model

Three layers, deliberately separated:

- **Raw** — `documents`, `ocr_results`, `extractions`. Immutable and versioned. This is
  the evidence trail; nothing here is ever edited in place.
- **Typed** — `receipts`, `invoices`, `line_items`. Per-document-type shapes with their
  own validation rules. This is what integrations map from.
- **Canonical** — `transactions`. The only table the analyst agent reads.

Plus `document_events` (audit), `jobs` (queue), `review_items`, `outbox`, and
`runs`/`steps` for per-model-call cost tracking.

**Built so far (phase 1):** `documents`, `ocr_results`, `document_events`, `jobs`.
Everything else is phase 2 or later.

### Why three layers rather than one table

Re-running extraction after a schema change must not require re-uploading or re-OCRing
the document. Separating raw from typed makes `reprocessing` a cheap operation over
stored OCR text. Separating typed from canonical means the analyst has one stable shape
to query regardless of how many document types exist.

### Money

`Decimal`, never `float`. Non-negotiable in a finance system.

### Document lifecycle

Each document has exactly one status; every transition appends a row to
`document_events`. States: `uploaded`, `duplicate`, `ocr_done`, `ocr_failed`,
`classified`, `unsupported`, `extracted`, `validated`, `needs_review`, `rejected`,
`committed`, `synced`, `reprocessing`.

Two deliberate separations:

- `committed` vs `synced` — third-party delivery fails independently of the ledger and
  must retry without touching it.
- `reprocessing` — exists because schemas change and historical documents get re-run
  from stored OCR text.

---

## 5. Decision record

Each entry: the decision, what was rejected, and what it costs.

### 5.1 Stack

**Python 3.12 · FastAPI · SQLAlchemy 2.0 · Alembic · SQLite → Postgres**

FastAPI gives OpenAPI docs for free, which makes the pipeline demonstrable in a browser
from day one. SQLAlchemy 2.0's `Mapped[...]` style is type-checkable. SQLite keeps local
development to zero services; the Postgres path is kept open by avoiding SQLite-specific
SQL and using batch-mode migrations.

**Cost:** SQLite's limitations are real and had to be worked around explicitly — see
5.8 and 5.9.

### 5.2 uv rather than pip + requirements.txt

**Deviation from the original plan.** `uv` gives a real lockfile (`uv.lock`), fast
resolution, and `uv run` without manual venv activation.

**Cost:** one more tool to install. `uvx` turned out to be an unplanned benefit — it ran
Datasette for database inspection with nothing added to the project.

### 5.3 Database-table job queue, not Redis or Celery

The `jobs` table is the queue. A worker claims a row with a conditional `UPDATE`.

**Rejected:** Redis + Celery/RQ. That adds a service to run, a second source of truth,
and a failure mode where the queue and the database disagree.

**Why this wins here:** the claim is *transactional*. A job and the document rows it
depends on are committed together, so a document is never visible without its job and
never has a job without being visible. A Redis list cannot offer that.

**Cost:** polling latency (~2s), and it will not scale to very high throughput. Both are
irrelevant at this volume. On Postgres the claim becomes
`SELECT ... FOR UPDATE SKIP LOCKED` for genuine multi-worker parallelism.

### 5.4 Content-addressed storage

A file's SHA-256 *is* its path: `raw/eb/ebf4f635…`.

**Consequence:** writing identical bytes twice is a no-op, so `storage.save()` is
idempotent without a lock. Re-uploading after a failed request cannot create a
duplicate. The same hash is the dedup key in the database, so one value does both jobs.

**Cost:** sha256 is byte-exact, not content-aware. Two *photographs* of the same receipt
have different bytes and will not dedupe. That is a real user-facing problem — see 8.6.

### 5.5 Magic-byte type detection, not the Content-Type header

The stored `mime_type` is detected from the file's leading bytes. The client's declared
`Content-Type` is ignored.

**Why:** it is client-supplied and trivially forged. **Verified:** uploading PNG bytes
while declaring `application/pdf` correctly stores `image/png`.

Handles JPEG, PNG, PDF, TIFF, WebP, and HEIC. HEIC matters specifically because it is
the iPhone camera default.

**Cost:** an unrecognised-but-valid format is rejected with `415` rather than accepted
optimistically. For untrusted uploads that is the right direction to fail.

### 5.6 File written before the database row

**Why this ordering:** a file with no row is harmless garbage a sweeper can collect. A
row pointing at a missing file breaks the worker. The failure modes are not symmetric,
so the ordering is not arbitrary.

### 5.7 UNIQUE on `sha256`, with the race handled explicitly

Two concurrent uploads of identical bytes both miss the `SELECT`, both `INSERT`, and the
unique index rejects one. That `IntegrityError` is caught and turned into "here is the
winner's document" rather than a 500.

**Rejected:** a lock around check-then-insert. The database already has the invariant;
enforcing it twice is worse than enforcing it once.

### 5.8 Constraint naming convention

`MetaData(naming_convention=...)` so every PK, FK, index, and unique constraint gets a
deterministic name (`uq_ocr_results_document_id`, `fk_jobs_document_id_documents`).

**Why it is not cosmetic:** SQLite has no meaningful `ALTER TABLE`, so Alembic rebuilds
tables in batch mode and must name every constraint it recreates. Anonymous constraints
cannot be reliably dropped.

**Concrete payoff:** phase 3 relaxes `ocr_results` from one-row-per-document to unique
`(document_id, engine)` so engines can be compared. That migration drops
`uq_ocr_results_document_id` *by name*. Without the convention it would be a hand-written
rename against a database-generated identifier.

This was added during a from-scratch rebuild of the schema (see 6.2) and was absent from
the first attempt.

### 5.9 `UTCDateTime` type decorator

SQLite has no datetime type. It stores whatever string it is handed and returns it
**naive**, dropping the offset. That broke two things at once:

1. Comparing the result to an aware `datetime.now(UTC)` raises
   `TypeError: can't compare offset-naive and offset-aware datetimes`.
2. Letting SQLAlchemy bind an aware value made SQLite compare
   `'2026-09-16 12:34:32.074008'` against `'2026-09-16 12:50:07.039837+00:00'` as
   **strings**.

The second is the more dangerous: it returned the correct answer for `<=` in UTC, so it
would have passed casual testing and then failed on a non-UTC offset or on Postgres.

`UTCDateTime` converts to UTC on write and tags as UTC on read. **Verified:** the
parameter now reaching the driver is naive (`'2026-09-16 12:51:46.647935'`), Python-side
comparison works, and `alembic check` confirms no migration is needed — the DDL type is
unchanged.

This was found by the worker's poll query (`run_after <= now`) and fixed before the
worker was written rather than discovered as a production crash.

### 5.10 Status as `String`, not SQLAlchemy `Enum`

`Enum` emits a `CHECK` constraint. This vocabulary gains members every phase, and each
addition would then require a migration. `DocStatus` (a `StrEnum`) is the source of
truth in Python; the column stores text.

**Cost:** the database will accept an invalid status string. Acceptable — the application
is the only writer.

### 5.11 UUIDs as `String(36)`

SQLite has no UUID type; Postgres accepts the same text. Moving to a native `uuid`
column later is one Alembic operation.

**Cost:** 36 bytes instead of 16, and slower joins at scale. Irrelevant at this volume.

### 5.12 OCR engine: PaddleOCR, with Tesseract as a test double

This was the largest open question inherited from the project handoff, which flagged
`github.com/baidu/Unlimited-OCR` as a candidate that could not be evaluated at the time.

**Evaluated and rejected: Unlimited-OCR.** It is a genuinely strong project — Baidu, MIT
licensed, released June 2026, ~25.7k stars, state of the art at **93.23%** on
OmniDocBench v1.5, and it emits bounding boxes via `<|det|>type [bbox]<|/det|>` markers.

Two reasons it is not the choice:

1. **It cannot run on the development machine.** Documentation tests on
   `python 3.12.3 + CUDA12.9`, inference examples call `.cuda()`, and the deployment
   paths are vLLM and SGLang — all NVIDIA. No documented Apple Silicon, MPS, or CPU
   path. The dev machine is an **Apple M5 Pro, 24 GB, Metal**. It is also a
   Mixture-of-Experts model at 30B total / 5B activated, which would be tight on 24 GB
   of unified memory even with a Metal backend.

2. **It would contradict design principle 1.** It is a vision-language model. Making it
   the OCR stage routes *every* document — including the easy majority — through a 30B
   VLM before anything else happens. The "cheap tier first, escalate to vision" argument
   collapses when the floor is the expensive tier. The README leads with cost per
   document; that number would be set by the most expensive component, running on
   everything.

**Chosen: PaddleOCR.** A classical detect-then-recognise pipeline. Runs on CPU at
practical speed, returns bounding boxes *and per-word confidence scores*, and
PP-StructureV3 adds layout and table analysis.

The confidence signal is worth more here than its raw accuracy: it is a free,
deterministic input to escalation logic. A VLM generates tokens and cannot supply it —
which is why `ocr_results.mean_confidence` is nullable, commented "null for VLM engines".

The layout capability matters for phase 2. A receipt is a two-column document
(description left, amount right). The extractor must know that `9.35` belongs to `TOTAL`
and not to `IVA 10%`, and that association comes from bounding boxes.

**Chosen: Tesseract as the test double.** Not a fallback — a different tool for a
different job. No GPU, no container, no network, installed via Homebrew. It makes tests
and CI trivial. It does not need to be accurate to do that.

**Unlimited-OCR is deferred, not discarded.** The `OcrEngine` protocol and the
`engine`/`engine_version`/`params` columns exist precisely so phase 3 can run both over
the same labeled set and publish the comparison. *"We evaluated a SOTA VLM against a
classical pipeline and chose the classical one on cost per document"* is a stronger
result than either engine alone.

**Honest caveat:** PaddleOCR's reported 98–99% page-level invoice accuracy is a
vendor/blog figure on general invoices, not measured on this project's data. Phase 3
exists to replace it with a real number.

### 5.13 The claim *is* the lock

Job claiming is one statement:

```sql
UPDATE jobs SET status='running', locked_by=?, locked_at=?, attempts=attempts+1
WHERE id = (SELECT id FROM jobs WHERE status='queued' AND run_after<=? 
            ORDER BY run_after LIMIT 1)
RETURNING id
```

**Rejected:** `SELECT` a queued job, then `UPDATE` it. That leaves a window where two
workers both see the same row.

There is no gap between deciding and taking, so there is no race to lose. Whichever
worker's statement lands first flips the row out of `queued`; every other worker's
`WHERE` clause stops matching and they claim nothing.

### 5.14 Permanent vs transient failure

The worker distinguishes two exception types, because the correct response differs:

- `UnsupportedMediaType` → **permanent**. The bytes will not become readable on a retry.
  Job goes to `failed`, document to `ocr_failed`, with the reason recorded.
- `OcrError` / `OSError` → **transient**. Exponential backoff (4s, 16s, 64s) until
  `max_attempts` is spent.

**Rejected:** retrying everything. Retrying a PDF that Tesseract structurally cannot read
burns three attempts to reach the same conclusion.

### 5.15 Stale job reclamation

A worker killed mid-job leaves its row in `running` forever and the document silently
never finishes. `reclaim_stale()` returns anything locked for over 15 minutes.

Crucially it does **not** reset `attempts`, so a job that reliably kills its worker still
exhausts `max_attempts` rather than looping indefinitely.

### 5.16 No code sandbox for extraction

A model returning JSON through a schema-enforced tool call cannot do anything but return
JSON. Sandboxing is reserved for two genuinely untrusted things: parsing uploaded files,
and phase 6's model-generated parser templates.

### 5.17 Per-type schemas, not one universal JSON

Receipts, invoices, and bank statements are different shapes with different validation
rules and different target tables. One universal schema would be mostly-null columns and
validation that cannot be specific enough to be useful.

### 5.18 Phase 3 before phase 5

The eval harness is built before LangGraph orchestration. Deliberate: it means the
orchestration layer can be justified with measurements rather than asserted, and the
README can show before/after numbers.

### 5.19 LangGraph as a component, not the architecture

The provider layer, tool registry, budgets, tracing, and evals all live *outside* the
graph. LangGraph arrives in phase 5 for what it is genuinely good at: batch fan-out,
conditional edges, and interrupt/resume at human review with checkpointing.

---

## 6. Deviations from the original plan

### 6.1 Package management: pip → uv

Predates this record. See 5.2. The README still described
`pip install -r requirements.txt` — a file that did not exist — until it was corrected.

### 6.2 The phase-1 schema was rebuilt from scratch

A previous session produced `core/models.py`, `core/status.py`, and an Alembic scaffold
that were never committed, and — critically — **the migration was never generated**. The
schema had never once been applied to a database.

The code was *correct* but could not be shown to be *complete*, because the session that
would have explained what was still coming was lost. Rather than build on an unverifiable
foundation, the work was reset to the last verified commit and rewritten.

**This was the right call, and the rebuild was measurably better:**

- Added the constraint naming convention (5.8), absent from the first attempt
- Generated, applied, and round-tripped the migration (`downgrade base` → `upgrade head`)
- Confirmed zero drift via `alembic check`
- Verified the one-OCR-result-per-document constraint actually rejects a second row,
  rather than assuming `unique=True` took effect

The discarded files were stashed rather than deleted, so the diff remained available.

### 6.3 `jobs` added to the schema

The handoff described a "DB-table job queue" as a concept but the ER diagram did not
include it. It is now a first-class table with `attempts`, `max_attempts`, `run_after`,
`locked_by`, `locked_at`, `last_error`, and a composite `(status, run_after)` index
serving the worker's poll.

### 6.4 Columns added beyond the original ER diagram

`documents` gained `original_filename` and `size_bytes`. `ocr_results` gained `id`,
`engine_version`, `params`, and `created_at`. `params` exists so an eval run can be
reproduced exactly from the row.

### 6.5 Unplanned additions

Not in the original plan, added because the work demanded them: magic-byte sniffing
(5.5), upload size cap and MIME allowlist, the `UTCDateTime` decorator (5.9), the
constraint naming convention (5.8), and atomic temp-file-plus-rename writes.

### 6.6 `api/deps.py` not created

Planned in the original layout. `Depends(get_session)` from `core.db` has been
sufficient; a one-item indirection layer would be noise. Will be added when there is a
second dependency.

---

## 7. Current progress

### 7.1 Verified working

Phase 1's core loop runs end to end:

```
upload → sha256 dedup → content-addressed store → job queued
       → worker claims → OCR → ocr_results → status transition → audit trail
```

**Endpoints:**

| Endpoint | Purpose |
|---|---|
| `POST /documents` | Multipart upload |
| `POST /documents/base64` | JSON upload for callers holding bytes |
| `GET /documents/{id}` | Status plus full audit trail |
| `GET /health` | Liveness (see 7.4) |

**Tested behaviours:** upload, re-upload deduping to the same id with no extra job
queued, base64 path, `415` on unrecognised bytes, `400` on empty file, `400` on invalid
base64, `404` on unknown id. Stored files were re-hashed and match their own filenames.

**Worker run over four documents:**

| File | Status | Result |
|---|---|---|
| `receipt.png` (1×1 px) | `ocr_done` | 0 blocks — nothing to read |
| `b64.png` (1×1 px) | `ocr_done` | 0 blocks |
| `IC MAYO 2026.pdf` | `ocr_failed` | Permanent: Tesseract cannot rasterise PDFs |
| `cafe-luna.png` | `ocr_done` | **28 blocks, mean confidence 0.865, 140 chars** |

The PDF failure is correct behaviour, not a gap: recorded with a reason, job marked
`failed` rather than retrying, document at `ocr_failed`.

### 7.2 Measured baseline: Tesseract

On a synthetic receipt (420×360 PNG, 17px Courier — *not* a photograph), Tesseract
returned 28 blocks at mean confidence 0.865, but misread the amounts:

```
5.00 → -00        8.50 → -50        9.35 → 35
```

It also flattened the two-column layout into reading order, detaching amounts from their
labels.

This is a real measurement and it is the baseline phase 3 must beat. It also directly
supports 5.12: on a finance document these are precisely the errors that must not reach
the ledger.

### 7.3 Repository state

```
11 commits · 1,320 lines of Python · working tree clean · pushed
Python 3.12.14 · FastAPI 0.141.1 · SQLAlchemy 2.0.52 · Pydantic 2.13.5 · Alembic 1.20.0
Migration 72a33d10bc7d (head) · alembic check: no drift · ruff: clean
```

### 7.4 Phase 1 remaining

```
[x] settings + DB session layer
[x] schema + migration (4 tables, verified)
[x] content-addressed storage + dedup
[x] POST /documents, POST /documents/base64, GET /documents/{id}
[x] OcrEngine protocol + Tesseract test double
[x] worker: claim, retry/backoff, stale reclamation
[ ] PaddleOCR container + HTTP shim
[ ] docker-compose.yml
```

---

## 8. Known gaps and deferred work

Stated plainly. These are known, not overlooked.

**8.1 No tests.** `pytest` is configured and collects zero tests. This is the most
significant gap — the pipeline has been verified by manual scripts, which is not
regression protection. Should be closed before the codebase grows further.
`core/db.py` builds its engine at import time from `get_settings()`, which will need a
small refactor to point tests at a throwaway database.

**8.2 `/health` is liveness only.** It runs `SELECT 1`, which succeeds against any
openable database — including one with zero tables. Demonstrated by rolling the migration
back to `base` and observing `200 {"status":"ok","db":"ok"}` against an empty schema. The
failure mode it cannot catch: deploy new code, forget to migrate, container reports
healthy, every real request 500s. The fix is to compare `alembic_version` against the
head revision the code expects. Deferred deliberately — it becomes load-bearing at phase
5, when schema bumps drive `reprocessing`.

**8.3 `DocStatus.DUPLICATE` is unused.** Because `sha256` is `UNIQUE`, a repeat upload
cannot create a second row, so the API returns the original with `duplicate: true` in the
response instead. But the README state machine specifies `uploaded --> duplicate`, which
implies a row holding that status. Unresolved inconsistency: either drop the status and
correct the README, or drop the unique constraint so repeats create rows marked
`duplicate`. Current preference is to drop the status, since the audit trail belongs in
`document_events`.

**8.4 No PDF support in the default dev path.** By design — Tesseract has no rasteriser
and should not guess. PaddleOCR handles PDFs. `pypdfium2` would be a pure-wheel option if
PDF support in the test double ever becomes necessary.

**8.5 No Docker Compose yet.** Lands with the PaddleOCR container.

**8.6 Content-level deduplication is unsolved.** `sha256` catches "you sent me this exact
file again." It does not catch "this is the same receipt." A user who photographs a
receipt, is unsure it uploaded, and photographs it again produces two different images of
one purchase — two OCR runs, two extractions, and eventually **two transactions for one
lunch**. Catching that requires comparison after extraction (same vendor, total, date,
tax ID → probable duplicate → review queue). Phase 4, and a genuinely different mechanism
from what exists.

**8.7 No cost tracking yet.** `runs`/`steps` and the per-extraction `cost_usd`,
`input_tokens`, `output_tokens` columns arrive with phase 2, when there is a model call to
measure.

**8.8 Security posture is incomplete.** Uploads are size-capped and type-checked, and
paths are derived from content hashes rather than user-supplied filenames (so path
traversal is structurally impossible). Not yet addressed: encryption at rest, PII
redaction before analyst context, and authentication — there is currently no auth at all.

---

## 9. Roadmap

| Phase | Ships | Demonstrates |
|---|---|---|
| **1** | Upload → dedup → OCR → stored text; status tracking; event log | Backend fundamentals, idempotency |
| 2 | Registry + `receipt` type: routing, schema-enforced extraction, validator, mapper | Core pipeline end to end |
| 3 | Eval harness on labeled receipts; per-field accuracy and cost per document | Measurement before optimisation |
| 4 | Vision fallback escalation, review queue with corrections, `invoice` type | Cost engineering, human-in-the-loop |
| 5 | LangGraph: batch fan-out, conditional edges, interrupt/resume at review, reprocessing | Orchestration where justified |
| 6 | Template-as-code: model generates per-vendor parsers in a sandbox, verified before promotion | Removing the model from the hot path |
| 7 | Analyst agent over `transactions`: read-only tools, citations, approval gate | Agent design as a data consumer |
| 8 | Outbox + integrations (CSV, then accounting API); UI | Production shape |

Phase 3 deliberately precedes phase 5 — see 5.18.

---

## 10. Reproducing the verification

Every claim in section 7 can be re-checked:

```bash
uv sync
uv run alembic upgrade head          # create the schema
uv run alembic check                 # assert models and schema agree
uv run ruff check .                  # lint
uv run uvicorn api.main:app --reload # API at :8000/docs
uv run python -m worker.run --once   # drain the job queue and exit
```

Inspect the database directly:

```bash
sqlite3 data/ledgerline.db ".tables"
uvx datasette data/ledgerline.db --port 8081   # browsable UI, read-only
```

---

## 11. Interview notes

Points in this project worth being able to defend, and where the reasoning lives:

- **Why not put a model in every step?** §2, §5.12, §5.16
- **How do you know it works?** §7.1, §7.2 — and honestly, §8.1: there are no automated
  tests yet, which is the real answer
- **Why a database queue instead of Redis?** §5.3 — transactional claims
- **How do you avoid double-processing?** §5.4, §5.7, §5.13
- **What happens when a worker dies mid-job?** §5.15
- **Why did you reject the state-of-the-art OCR model?** §5.12 — hardware *and*
  architecture, and the second reason is the interesting one
- **What went wrong and what did you do about it?** §5.9 (a silent correctness bug caught
  before it shipped), §6.2 (discarding unverifiable work rather than building on it)
- **What is still broken?** §8, all of it

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
                    extractions.raw_json ──> transactions (canonical)
                              │
                    analyst agent · integrations · exports · review queue
```

### Component boundaries

| Boundary | Why it exists |
|---|---|
| `OcrEngine` protocol | Engines are swappable and, critically, *comparable* — phase 3 runs several over one labeled set |
| `Extractor` protocol (phase 2) | Keeps provider and tier choice out of business logic, so the eval harness can swap either over one labeled set and the worker never learns which model ran |
| Document type registry (phase 2) | A document type is a row, not a module: adding one needs no deploy, and versioning is a new row — see 5.20 |
| Three data layers | Raw is immutable evidence, typed is the extracted JSON, canonical is the single thing the analyst reads |

---

## 4. Data model

Three layers, deliberately separated:

- **Raw** — `documents`, `ocr_results`. Immutable. This is the evidence trail; nothing
  here is ever edited in place.
- **Typed** — `extractions.raw_json`, shaped by a `document_types` row. Append-only and
  versioned: a retry, a stronger model or an edited definition writes a new row rather
  than overwriting, and `is_current` marks the one that counts.
- **Canonical** — `transactions`. The only table the analyst agent reads.

Plus `document_events` (audit), `jobs` (queue), `review_items`, `outbox`, and
`runs`/`steps` for per-model-call cost tracking.

**Built so far:** `documents`, `ocr_results`, `document_events`, `jobs` (phase 1);
`document_types`, `extractions`, `transactions` (phase 2). `outbox`, `review_items` and
`runs`/`steps` are later.

The typed layer is JSON rather than per-document-type tables — see 5.20 for why, and
what that costs.

### Why three layers rather than one table

Re-running extraction after a schema change must not require re-uploading or re-OCRing
the document. Separating raw from typed makes `reprocessing` a cheap operation over
stored OCR text. Separating typed from canonical means the analyst has one stable shape
to query regardless of how many document types exist.

### Money

`Decimal`, never `float`. Non-negotiable in a finance system.

### Document lifecycle

Each document has exactly one status; every transition appends a row to
`document_events`. States: `uploaded`, `ocr_done`, `ocr_failed`,
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

**Chosen: the PP-OCR models, run on ONNX Runtime.** A classical
detect-then-recognise pipeline. Runs on CPU at practical speed, returns bounding boxes
*and per-word confidence scores*.

A second hardware constraint forced a refinement here. **PaddlePaddle publishes no
`linux_aarch64` wheel** — only `manylinux1_x86_64`, `macosx_11_0_arm64`, and
`win_amd64`. The development machine is ARM and a container is Linux, so that
combination does not exist. Two options remained:

1. Force the container to `linux/amd64` and run the official wheel under emulation.
   Verified that amd64 emulation works on this Docker, but PaddlePaddle's x86 wheels
   expect AVX, which Rosetta does not emulate — likely slow QEMU fallback or an
   outright crash, plus a ~2.5 GB image.
2. Run the same PP-OCR detection and recognition models exported to ONNX, via
   `rapidocr` (a pure-Python wrapper) on `onnxruntime`, which *does* ship ARM Linux
   wheels.

**Option 2 was chosen.** Native speed, ~400 MB image, and identical outputs — bounding
boxes and per-word confidences. The models are the same; only the runtime differs.

**The honest claim is therefore "PP-OCR models on ONNX Runtime", not "PaddleOCR".** The
engine reports itself as `ppocr-onnx`, and `EngineType` in the wrapper does expose a
`paddle` backend should the official runtime ever become viable on this hardware.

The confidence signal is worth more here than its raw accuracy: it is a free,
deterministic input to escalation logic. A VLM generates tokens and cannot supply it —
which is why `ocr_results.mean_confidence` is nullable, commented "null for VLM engines".

The layout capability matters for phase 2. A receipt is a two-column document
(description left, amount right). The extractor must know that `9.35` belongs to `TOTAL`
and not to `IVA 10%`, and that association comes from bounding boxes. **Verified:** PP-OCR
returns `TOTAL` and `9.35` as two separate detections with their own boxes, where
Tesseract flattened them into one reading-order stream.

Two findings from configuring it, both worth recording:

- **`lang_type: latin` raises `ValueError` for PP-OCRv6 small.** Pinning it, which
  seemed obviously right for Spanish documents, would have crashed the container at
  startup. The default (`ch`) model is the multilingual one and reads Spanish
  accents and comma decimal separators correctly.
- **Models are baked into the image**, so the container needs no network at runtime and
  works offline.

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

A model returning JSON against an enforced schema cannot do anything but return JSON.
Sandboxing is reserved for two genuinely untrusted things: parsing uploaded files, and
phase 6's model-generated parser templates.

Implementation note: this is done with structured outputs (`output_config.format`), not a
forced tool call. The API constrains the response to the JSON Schema directly, which is
what smuggling the schema through a tool definition was always a workaround for. One
fewer indirection, and no tool-call plumbing to parse back out.

### 5.17 Per-type schemas, not one universal JSON

Receipts, invoices, and bank statements are different shapes with different validation
rules. One universal schema would be mostly-null fields and validation that cannot be
specific enough to be useful.

Per-type *schemas* no longer implies per-type *tables*, though — see 5.20.

### 5.18 Phase 3 before phase 5

The eval harness is built before LangGraph orchestration. Deliberate: it means the
orchestration layer can be justified with measurements rather than asserted, and the
README can show before/after numbers.

### 5.19 LangGraph as a component, not the architecture

The provider layer, tool registry, budgets, tracing, and evals all live *outside* the
graph. LangGraph arrives in phase 5 for what it is genuinely good at: batch fan-out,
conditional edges, and interrupt/resume at human review with checkpointing.

Since the roadmap was reordered (§9), only where it earns that. The `jobs` table already
fans out and review is a status, so the candidate is the template agent's draft → trial →
revise loop, adopted if it measures better than a plain loop.

### 5.20 A document type is data, not code

The original design gave each document type a Python module satisfying a `DocumentType`
protocol — `matches()`, `extraction_prompt()`, `validate()`, `map_to_rows()` — and the
README advertised "adding a document type means adding one module."

It is now a **row**. `document_types.definition` holds a JSON document — fields with
their extraction descriptions, deterministic matchers, a validation rule list, and
per-issuer description overrides — and one generic processor interprets all of it.

**What this buys:**

- **Adding a type needs no deploy.** This is the precondition for phase 6's authoring UI.
  Under a protocol-per-type design, "let an analyst add a vendor" means "let an analyst
  ship Python," which is a sandbox problem nobody wants to own.
- **Versioning is nearly free.** A definition is a value, so a new version is a new row
  and extractions produced by the old one keep their meaning. Versioning a module means
  versioning code and keeping both importable.
- **The format validates itself.** `Definition` is a Pydantic model, so a malformed
  definition is rejected with a precise error at authoring time rather than failing
  halfway through an extraction — and a rule naming a field that does not exist is caught
  immediately instead of silently never firing on any document.
- **Per-issuer tuning without forking the type.** A `Profile` overrides *descriptions*
  only; it cannot add or remove fields, because the target shape belongs to the document
  type and only the layout belongs to the issuer.
- **The authoring UI is generated, not written.** `Definition.model_json_schema()`
  describes the format, so the form follows from the model.

**What it costs:**

- **Expressiveness.** Rules are a closed vocabulary of seven types, not an expression
  language. Deliberate: a fixed set is pickable from a dropdown and independently
  testable, whereas an embedded expression language is a parser, a sandbox, and a
  debugging surface. The one place a type does carry code is its transform (§5.23),
  which computes values rather than judging them.
- **No per-type tables, therefore no per-type columns.** `extractions.raw_json` is the
  typed layer. Querying JSON is worse than querying a column, and that is the trade — a
  field is promoted to a real column only when code must act on it, which is exactly what
  the canonical `transactions` table is.
- **Descriptions are load-bearing prose.** The single most important string in the system
  is a `description` field, and nothing type-checks prose. A bad description degrades
  extraction silently. This is precisely what the phase-3 eval harness exists to catch,
  and it is the strongest argument for building it before anything is scaled up.

### 5.21 The canonical mapping is data too

A validated extraction has to become a row in `transactions`, and the obvious way to
write that is a function per document type -- `map_to_rows(data) -> Transaction`. That
is the design 5.20 rejected one layer up, and it fails here for the same reason: an
analyst adding a document type through the UI cannot ship Python.

So the mapping is a block in the definition. `canonical.amount: total` says which field
holds the amount, and one generic mapper reads it.

Two things fall out of that choice. The references are **validated against the field
set at authoring time**, including their types -- `canonical.amount` pointing at a
string field is rejected when the definition is saved, not on the first document that
reaches the ledger. And a type with **no** `canonical` block is extracted and validated
and then stops, which is the correct behaviour for a type someone is still shaping
rather than an error to handle.

`direction` is a constant rather than a field reference, because it is a property of
the type and not of the document: every purchase receipt is money leaving. A type that
could genuinely be either way round wants to be two types.

**What the ledger deliberately does not do is infer.** A receipt that prints no
currency produces a null currency, not EUR-because-it-is-Spanish. A currency field
holding "Euros" is dropped rather than mangled into three characters. The raw answer
is in `extractions.raw_json` either way, and the value of a canonical table is that
everything in it was read off a page -- which is also what makes the analyst agent's
citations worth anything.

### 5.22 API keys, and what they deliberately do not do

The API had no authentication at all, which 8.8 recorded and nothing fixed. It does now:
a key, hashed, presented as `Authorization: Bearer`.

**This is authentication, not authorisation.** No table has an owner column -- no
`user_id`, no `tenant_id` -- so every key that gets in sees every document and every
ledger row. That is a locked front door, and calling it access control would be a lie.
Per-caller isolation means adding tenancy to the data model first, which is a much
larger decision and should not arrive smuggled in behind a login.

**The hash is SHA-256, not bcrypt or argon2.** Slow hashes exist because human-chosen
passwords have little entropy and must be expensive to guess. A key from
`secrets.token_urlsafe(32)` carries 256 bits, so there is nothing to brute force and no
reason to pay for slowness on every request. Lookup is *by* hash, so there is also no
secret-dependent comparison to leak timing.

**The dependency is on the app, not the router.** A route added later is protected
because nobody did anything, rather than exposed because somebody forgot; opening one
means editing `PUBLIC_PATHS`, which is a line a reviewer sees in a diff. A test adds a
route without mentioning auth and asserts it is still shut.

**Absent, malformed, unknown and revoked all answer the same.** Distinguishing them
tells an attacker whether a key exists and tells an honest caller nothing that "check
your key" does not.

`/health` stays public: a load balancer cannot hold a credential, and refusing it would
make an unauthenticated deploy look healthy and an authenticated one look dead. The docs
are open because this is a project meant to be read; a real deployment would close them.

`last_used_at` is written at most once a minute. Recording it on every request turns
every GET into a write, and SQLite serialises writers -- once a minute is plenty to
answer "is anyone still using this key".

**People came later, for the panel (§9.1).** Keys stay for machines; people sign in
with a password and carry a session cookie. Both become one `Principal` with a role, so
no route knows or cares which door a caller used.

- **Passwords are argon2id; keys and session tokens are SHA-256.** The difference is the
  same argument as above, run the other way. A password is low-entropy and reused, so
  each guess against a stolen table should cost real time and memory. That cost is paid
  once per sign-in, and the session token it mints is random, so per-request checks go
  back to a fast hash.
- **Sessions are server-side rows, not signed cookies.** Signing out, disabling a user or
  changing a password ends the session at once; a signed token could only wait to
  expire. That is also the answer to "why not a JWT": revocation is the requirement, and
  a JWT needs a denylist to revoke, which puts the database lookup back.
- **The cookie is HttpOnly, Secure and SameSite=Lax, and anything that changes state
  needs a CSRF token** sent in a header, which a page on another site cannot read or
  set. A request carrying an API key skips the check, because a browser never attaches a
  key by itself.
- **Every sign-in failure answers the same**: wrong password, unknown email, disabled or
  locked. An unknown email still costs one hash, so timing doesn't separate them either.
  Ten failures in a row lock the account for fifteen minutes.
- **Roles decide what a caller may change.** An *author* drafts and trial-runs; an *admin*
  also publishes and retires, which is what puts a transform's code live. Keys carry a
  role too: a drafting agent's key can propose but never publish.

Still authentication plus two roles, not tenancy. Everyone who gets in sees everything.

### 5.23 Transforms: the one place a type carries code

The model reads and code computes. That left a gap: an invoice printing a subtotal and
tax but no total comes back with the total null, correctly, and the ledger can't take a
row without an amount. Working the total out is arithmetic. What to work out depends on
the format, so it belongs to the document type, and no fixed rule vocabulary covers it.
So a type may carry a **transform**: Python defining `transform(data) -> data`, run on
each extraction after coercion and before the rules.

The code can sit inline in the definition or in a `.py` file beside the seed. The loader
reads that file into the definition, so **the stored version carries the code itself**.
A version is a version of the code too, and an extraction's `schema_version` still says
exactly what ran.

**Who writes it decides how it runs.** Transforms are written by people, not generated,
so the threat is mistakes rather than attacks. They run in a child interpreter
(`python -I -S`): standard library only, an empty environment (so not the API key), a
scratch working directory, a time limit, and a CPU and memory cap on Linux. That is
containment: an infinite loop or a crash costs one document, not the worker. It is not
a sandbox against hostile code, and phase 6's generated templates would need a real one
(§5.16). Running in-process would have been faster, about 25 ms less per document, but
then one bad `while` stalls the queue.

**Nothing derived passes for read.**

- `extractions.raw_json` stays exactly what the model returned. The transform's output
  goes in `transformed_json` beside it, and `derived_fields` lists every field whose
  value differs from the model's reading. Reformatting doesn't count: `4.5` and `4.50`
  are the same money.
- `transactions.derived_fields` lists the ledger columns that came from those fields,
  e.g. `["amount"]` for a computed total. The analyst agent sees it and is told to say
  when it quotes a worked-out figure.
- The eval harness scores `raw_json`, the model's reading, so a transform can't inflate
  extraction accuracy.

**Its output is held to the same standard.** It is coerced against the same field types
and checked by the same rules, so a transform can't produce a value its own type would
reject. A transform that raises or times out doesn't fail the job: the document goes to
`needs_review` with the exception and line number, and the model's reading is kept.
Retrying wouldn't help, because the fault is in the type.

**Broken code never becomes the active version.** A transform declares examples (an
input and the fields expected out), and `seed.sync` runs them for every type it is
about to write, before writing any. Validating a definition only parses the code and
never executes it, because definitions are validated on every load.

The first real one is the invoice's: if no total is printed but a subtotal is, total =
subtotal − discount + tax. The tax is the printed figure, else the sum of the tax lines.
A printed total is never touched, and no subtotal means nothing is derived.

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

### 6.7 The document type registry became data

The plan, and the README, specified a `DocumentType` protocol implemented once per type.
Phase 2 replaced it with definitions stored as rows — before any type had been written
against the protocol, so nothing was discarded. Reasoning in 5.20.

Two knock-ons. The ER diagram's `receipts`, `invoices` and `line_items` tables were never
built and are no longer planned; `extractions.raw_json` replaces them. And the planned
`ChatProvider` protocol became `Extractor` (`llm/base.py`), narrowed to the one call
phase 2 actually makes — extraction — rather than a general chat abstraction built ahead
of a second caller. The analyst agent in phase 7 is that second caller, and can widen the
boundary when it exists.

The README described the rejected design until this record was updated.

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
| `GET /health` | Liveness only — see 8.2 for what it cannot catch |

**Tested behaviours:** upload, re-upload deduping to the same id with no extra job
queued, base64 path, `415` on unrecognised bytes, `400` on empty file, `400` on invalid
base64, `404` on unknown id. Stored files were re-hashed and match their own filenames.

**OCR sidecar:** the `ocr` container runs the PP-OCR models on ONNX Runtime behind a
small FastAPI shim (`POST /ocr`, `GET /health`). It rasterises PDFs with `pypdfium2`,
returns per-block bounding boxes, detection polygons and confidences, and reports
readiness rather than mere liveness. Hardened: `read_only` filesystem, `cap_drop: ALL`,
`no-new-privileges`, tmpfs scratch, no credentials, no network needed at runtime.

**Worker run over four documents, both engines:**

| File | Tesseract | PP-OCR on ONNX |
|---|---|---|
| `receipt.png` (1×1 px) | `ocr_done`, 0 blocks | `ocr_done`, 0 blocks |
| `b64.png` (1×1 px) | `ocr_done`, 0 blocks | `ocr_done`, 0 blocks |
| `IC MAYO 2026.pdf` | **`ocr_failed`** — cannot rasterise | **`ocr_done`, 23 blocks, conf 0.955**, 7.7s at 200 DPI |
| `cafe-luna.png` | `ocr_done`, 28 blocks, conf 0.865 | `ocr_done`, 14 blocks, **conf 0.999** |

Under Tesseract the PDF failure was *correct* behaviour rather than a gap — recorded with
a reason, job `failed` rather than retrying, document at `ocr_failed`. The container
removes the limitation rather than the error handling.

Re-running the same documents through a second engine required deleting the existing
`ocr_results` rows, because of the `UNIQUE(document_id)` constraint. That is exactly the
constraint phase 3 relaxes to `(document_id, engine)` — see 5.8.

### 7.2 Measured baseline: Tesseract

On a synthetic receipt (420×360 PNG, 17px Courier — *not* a photograph), Tesseract
returned 28 blocks at mean confidence 0.865, but misread the amounts:

```
5.00 → -00        8.50 → -50        9.35 → 35
```

It also flattened the two-column layout into reading order, detaching amounts from their
labels.

PP-OCR on ONNX Runtime, same image, read **every amount correctly** at 0.999 mean
confidence:

| Field | Tesseract | PP-OCR on ONNX |
|---|---|---|
| `5.00` | `-00` | `5.00` |
| `8.50` | `-50` | `8.50` |
| `9.35` | `35` | `9.35` |
| Mean confidence | 0.865 | **0.999** |
| Column structure | flattened to reading order | preserved as separate boxes |

On a Spanish receipt it also handled accents (`PANADERÍA`, `rústica`, `Málaga`) and
comma decimal separators (`1,20`, `3,60`), missing only one accent (`Andalucía` →
`Andalucia`).

These are real measurements on *synthetic* images, not photographs, and they are a
baseline rather than an eval — phase 3 replaces them with per-field accuracy on a labeled
set. They directly support 5.12: on a finance document, Tesseract's errors are precisely
the ones that must not reach the ledger.

**On the real PDF**, printed text read cleanly while **handwritten** fields degraded
badly (a handwritten date read as `19105/2026`). Handwriting is a separate problem and is
not currently in scope.

### 7.3 Repository state

```
17 commits · 3,224 lines of Python + 3,217 lines of tests · phase 2 uncommitted
Python 3.12.14 · FastAPI 0.141.1 · SQLAlchemy 2.0.52 · Pydantic 2.13.5 · Alembic 1.20.0
anthropic 1.5.0
Migration 6823df7fd6b2 (head, applied) · alembic check: no drift · ruff: clean
pytest: 742 passed in 5.1s
```

### 7.4 Phase 1 checklist

```
[x] settings + DB session layer
[x] schema + migration (4 tables, verified)
[x] content-addressed storage + dedup
[x] POST /documents, POST /documents/base64, GET /documents/{id}
[x] OcrEngine protocol + Tesseract test double
[x] worker: claim, retry/backoff, stale reclamation
[x] OCR container (PP-OCR on ONNX Runtime) + HTTP shim
[x] docker-compose.yml
[x] PDF support via pypdfium2 rasterisation
```

### 7.5 Phase 2

1,697 lines across two new packages (`doctypes/`, `llm/`) and the extract stage in the
worker, plus two tables, under **235 new tests**:

| Module | Provides |
|---|---|
| `doctypes/definition.py` | The definition format: `FieldSpec`, `Matchers`, seven rule types, `Profile`, and a `Definition` validator that rejects rules or profiles referencing fields that do not exist |
| `doctypes/schema.py` | `build_json_schema()`, `match_score()`, `select_profile()` |
| `doctypes/validate.py` | Coercion (money to `Decimal`, dates, nested lists) and the evaluator for all seven rule types |
| `doctypes/registry.py` | `active_definitions()`, `route()` — loads types from the table and picks the highest scorer |
| `core/repository.py` | `create_document_type_version()`, `get_active_document_type()`, `record_extraction()` |
| `worker/run.py` | The `extract` stage: route, build the schema, call the model, validate, record |
| `llm/base.py` | `Extractor` protocol; `ExtractionResult` carrying tokens, cost and latency as results rather than logs |
| `llm/anthropic.py` | `AnthropicExtractor` via structured outputs, with a permanent/transient error split so the worker retries only what can succeed |
| `core/models.py` | `document_types`, `extractions` — migration `6823df7fd6b2`, applied |

Remaining before the phase-2 loop closes:

```
[x] rule evaluator — all seven rule types, 120 tests
[x] coercion — money strings to Decimal, dates to date
[x] registry — load definitions from the table; route across active types
[x] tests for doctypes/ and llm/ — 219 of them
[x] worker wiring — `extract` is a second job kind, 21 more tests
[x] a seeded `receipt` definition shipped with the repo
[x] the mapper and `transactions` -- `committed` is reachable
```

**The loop closes.** A real worker process, against a real API, on a seeded Spanish cafe
receipt: `classified -> extracted -> validated`, every field correct, no validation
errors, $0.001377 over 922 input and 91 output tokens, 3.1s. The `line_items_sum` rule
passed with the tax difference inside tolerance, and `cost_usd` came back out of SQLite
as a `Decimal`.

Worth recording from that run: the `issued_on` description named the day/month/year
convention explicitly, and `12/05/2026` came back as `2026-05-12`. That is the cheap fix
for the date-inference trap above, and it works -- but it is a per-field instruction
somebody has to remember to write, which makes it an eval-harness concern rather than a
solved problem.

**The shipped seed, same loop, real call.** `doctypes/seeds/receipt.yaml` against a
twelve-line Spanish till roll: `classified -> extracted -> validated`, nine fields and
three line items all correct, five rules passed, $0.002731 over 1,981 input and 150
output tokens in 4.3s.

Three things that run showed. Two have since been fixed; the third is phase 3's:

- **The descriptions are most of the input cost.** Input went from 922 tokens to 1,981
  between the throwaway definition and the shipped one, for the same receipt. The field
  descriptions are the prompt, so writing them better and writing them longer are the
  same action, and only measurement separates the two. Deduplicating descriptions across
  the `anyOf` branches has since taken it to 1,667, but the shape of the problem stands.
- **The model inferred a currency the receipt does not state.** *Still open.* Nothing in
  the text carries a symbol or code, the description says "Null if nothing on the receipt
  says", and the system prompt says never infer -- and it answered `EUR`, from the fact
  that the receipt is Spanish. Defensible, probably even wanted, but it is not what was
  asked for. Tightening the description is a guess at a fix; counting how often it
  happens is the eval harness's job, which is where this one waits.
- ~~**Optional list item fields come back absent, not null.**~~ Fixed, and confirmed on a
  second live call: all three line items now carry a `quantity` key. Cost fell from
  $0.002731 to $0.002312 for the same receipt in the same pass.

Two correctness traps the tests pinned, both worth knowing about:

- **`"1,234"` must be rejected, not normalised.** Replacing the comma with a period turns
  1234 into 1.234 and loses three orders of magnitude silently. The money coercion
  validates against the wire pattern *before* swapping separators, so a thousands
  separator, a currency symbol or a third decimal is reported rather than guessed at.
- **Four `anthropic` exception families escaped `extract()` uncaught** and reached the
  worker with no retryable/permanent classification: `APIResponseValidationError` (an
  `APIError`, not an `APIStatusError`), `RetryableError`, and the two credential errors.
  `RetryableError` is raised once the SDK has spent its own retries, so the one exception
  named for the worker's job was the one the worker could not see. Now mapped to
  `ExtractionError` by a trailing `except anthropic.AnthropicError`.

**Both earlier unknowns are now settled**, by a live call on 2026-10-14 against
`claude-haiku-4-5`:

- **The generated JSON Schema is accepted.** `format: "date"`, the money `pattern`, and
  `anyOf` with a sibling `description` were each probed in isolation, and all three are
  accepted by structured outputs — as is the full generated `receipt` schema.
- **One extraction has run end to end.** A ten-line Spanish cafe receipt returned every
  field correctly — total, ISO date, vendor, three line items — in 3.4s for **$0.001205**
  (790 input, 83 output tokens). The hard-coded price table reproduces that figure to the
  cent, so `price()` is now confirmed against a real call and not only against the
  published rates.

Three things that call surfaced, none of them blocking:

- **Money arrives with the separator as printed** (`"7,15"`, `"1,80"`), exactly as the
  system prompt asks. The comma-to-period normalisation in the coercion layer is
  load-bearing, not defensive.
- ~~**List item fields are optional in a way top-level fields are not.**~~ Fixed. The
  nullable rule now lives in one function that both the top level and `items` go
  through, so it cannot drift apart again. Deduplicating the description across the two
  `anyOf` branches while in there took 366 tokens off every call -- 27% of the schema.
- **`format: "date"` quietly demands inference.** The receipt printed `12/05/2026` and the
  model returned `2026-05-12`. Right for Spain, wrong for the identical receipt issued in
  the US — and it sits awkwardly beside the system prompt's "never infer, calculate, or
  invent a value." Ambiguous dates are a silent failure mode; a field description naming
  the expected convention is the cheap fix. One for the eval set.

Deliberately absent: extended thinking on the extraction call. Extraction is perception,
not reasoning — the answer is on the page — so thinking would buy latency and tokens for
a task whose difficulty is reading. Worth revisiting only if the eval harness shows the
cheap tier failing on documents where the arithmetic, not the reading, is what is hard.

### 7.6 Phase 3 in progress — the eval harness

The harness exists; the corpus does not. That ordering is deliberate: the
measuring instrument should be trustworthy before it produces a number anyone
acts on, and it is testable without a single real receipt.

| Module | Provides |
|---|---|
| `evals/labels.py` | The label format and loader, plus `check` -- a corpus is validated before a run starts, because every document costs a model call |
| `evals/score.py` | Per-field scoring, the five outcomes, and aggregation across a run |
| `evals/run.py` | The runner, the report, `--compare` and `--draft-labels`; `python -m evals.run` |
| `evals/draft.py` | Model output written into label files for a human to correct |
| `evals/examples/` | Three synthetic labelled receipts, committed |

**The unit is a field, not a document.** "87% of documents were perfect" hides
which field is costing the 13%, and per-type schemas exist precisely because
fields fail independently: a vendor name that is nearly always right and a tax
id that is nearly always wrong are two problems with two different fixes.

**Five outcomes, because "wrong" is not one thing.** `correct`, `absent_ok`,
`missed`, `hallucinated`, `wrong`. The pair that earns its keep is `missed`
versus `hallucinated`. A missed field is a description that needs to point
harder at the page. A hallucinated one is a model answering from the world
rather than from the document, which no description fixes and which a finance
system cannot tolerate quietly -- an invented tax id is indistinguishable from
a real one downstream.

Both sides are coerced through the same code the pipeline uses, so `'7,15'`
and `'7.15'` and `7.15` are one value and a label never has to guess which
spelling the model will choose. Case and stretched whitespace are forgiven;
accents are not, because a different letter is a different answer.

**Routing is exercised, not bypassed.** The label says which type a document
should be and the registry is allowed to disagree. A misrouted document is
scored as a routing failure with no fields, and routing accuracy is reported
separately -- it has a different cause and a different fix from a field being
wrong.

**Comparing two variants is the point, not a feature.** A single number says
nothing actionable; the question is always "did this change help". `--compare`
runs one corpus under two models and diffs them, and the report leads with
regressions rather than the headline, because an overall figure that improved
can still hide a field that broke -- a model that learns to read tax ids and
forgets to read totals has not improved.

The corpus is prepared once and shared between variants: text is resolved and
routing decided before either model is called. Re-resolving per variant would
let OCR nondeterminism surface as a difference between models, which is
exactly what a comparison is supposed to rule out.

**Labelling is correction, not transcription.** `--draft-labels` runs the
pipeline over unlabelled documents and writes what it found into label files.
Most of labelling is transcription and the extractor already does that, so a
person corrects rather than types.

Every draft lands `verified: false`, and the runner refuses to score an
unverified label. The default is `false` even for a hand-written label, which
is mild friction in exchange for the one guarantee that matters: a draft
cannot silently become ground truth. Without it the model marks its own
homework and scores 100%, and the number looks exactly like evidence.

Validity is checked before verification -- a malformed label is broken whether
or not anyone signed it off, and reporting "unverified" at someone holding a
label with a typo in it helps nobody.

**Why the flag is not paranoia.** Drafting a rendered receipt through
Tesseract produced this, and every line of it reads plausibly:

| On the page | OCR read | The draft said |
|---|---|---|
| `07/03/2026` | `07/09/2028` | `issued_on: '2028-09-07'` |
| `TOTAL 2,90` | `TOTAL 280` | `total: '2.80'` |
| `CIF B87654321` | `CIF 87854321` | `tax_id: '87854321'` |

The extractor was not at fault -- it transcribed what OCR gave it rather than
repairing it, which is what the system prompt asks for. But a reviewer
skimming that file for plausibility would approve a date wrong by two years
and a total ten cents light. Checking has to mean looking at the document,
which is why the draft header says so in those words.

**The corpus is private.** Real receipts carry real vendor names, tax ids and
amounts, and this repository is public. `eval_corpus_dir` defaults to
`data/evals/`, which is already gitignored. The three examples in
`evals/examples/` are synthetic and committed, so the format is documented and
the runner is demonstrable without anyone's real data.

**First run, against those three examples:**

```
field accuracy    97.7%        routing accuracy  100.0%
cost              $0.002171 per document        latency  1523ms mean

invented values (the document said nothing):
  cafe-sunrise-001  currency = 'EUR'
```

**That is not a baseline and should not be quoted as one.** Three synthetic
documents, written by the same person who wrote the field descriptions, is a
proof that the instrument works -- nothing more. It does establish one thing
worth having: the currency invention from 7.5 is now reproducible on demand
and attached to a named document, rather than being a thing that happened once.

**First comparison, Haiku 4.5 against Sonnet 5 over the same three examples:**

```
no regressions
field accuracy      97.7%      97.7%   +0.0%
cost / doc      $0.002171  $0.006449

claude-sonnet-5 cost 3.0x more and moved field accuracy +0.0%
```

Again: three synthetic documents prove the instrument, not the tiers. But one
detail is worth more than the tie. Sonnet made *the same* currency invention
on *the same* receipt. If the stronger model fails identically, the cause is
unlikely to be capability -- which points at the description or the prompt
rather than at the tier, and makes escalating to a stronger model a poor fix
for this particular failure. That is the shape of conclusion the harness is
supposed to produce, and it is unavailable from a single run at a single tier.

Of the two example receipts that print no currency, one was invented for and
one was not. The sparse till roll was answered `null` correctly; the full
Spanish ticket was answered `EUR`. A plausible reading is that the surrounding
Spanish context is doing the inferring, which would mean the fix is a
description that names the absence rather than a stricter prompt -- but two
documents cannot distinguish that from chance, which is exactly why the real
corpus has to come before the fix.

### 7.7 Phase 7 — the analyst agent

Taken out of order. It was blocked on `transactions`, which arrived with the
ledger, and unlike everything in phase 3 or 4 it needs no labelled corpus.

| Module | Provides |
|---|---|
| `analyst/tools.py` | Six read-only queries over the ledger, and the arithmetic |
| `analyst/agent.py` | The loop, the tool schemas, and the system prompt |

**The model never does arithmetic.** That is the whole claim. A model asked
"what did I spend at Mercadona" that answers from figures in its context will
occasionally be wrong, and wrong invisibly -- the answer reads exactly like a
correct one. So the sums happen in `analyst/tools.py` under test, and the
model's contribution is choosing the tool and saying the answer.

**Read-only is a capability, not a rule.** There is no tool that writes, so
"requires human approval" is not an instruction the model is asked to honour
-- it is something it cannot do. A test asserts that running every tool
leaves the table byte-identical, and another rejects any future tool whose
name suggests a write. The agent can describe a change; applying one is a
separate call a person makes.

**Totals never cross currencies.** `8.30 EUR` and `8.30 USD` cannot become
`16.60` of nothing, so a total comes back split by currency and a mixed
result carries a note saying not to add it up. Rows whose currency the
document never stated get their own bucket rather than being folded into the
majority -- the same refusal to infer that the mapper makes in 5.21.

**Citations come off the tool results, not the prose.** The model can
misreport a document id; it cannot invent one into `Answer.sources`, because
those are collected from what the tools returned.

**The loop is written out rather than delegated** to the SDK's tool runner.
Forty lines, no beta dependency, and being able to read exactly what is sent
is worth more here than the lines it saves: this is the part of the system a
reviewer will want to audit. A failing tool is reported back to the model as
an error result rather than swallowed, because dropping it leaves the model
waiting for an answer that never comes.

**A live run, five transactions in three currencies:**

```
Q: What did I spend in total?

   Split by currency since they can't be combined:
   - EUR: 66.65 (3 transactions)
   - USD: 95.00 (1 transaction)
   - Unstated currency: 12.00 (1 transaction -- the document didn't specify)

   tools: ledger_summary -> total_spend    cites: 5 documents    $0.014
```

It oriented itself before querying, refused to combine the currencies, gave
the unstated one its own line, and cited every document. A second question
fired three tools in one turn and drilled into individual rows. Both sums
were computed in SQL.

### Two bugs, and why the fixes are not where the bugs were

Both were the same shape: an implicit contract across a module boundary,
enforced by nothing. Neither was a logic error -- in each case one module
produced a shape and another assumed a different one. So both were fixed at
the contract rather than at the instance.

**Citations could be injected.** `get_source` nested the document id one
level down, so a drill-down returned an answer citing nothing. Patching that
one field would have missed the real problem: citations were recovered by
walking tool payloads for keys named `document_id` or `sources`, and
`get_source` embeds `extraction.raw_json`. A document type may declare any
`^[a-z][a-z0-9_]{0,48}$` field name, so **content read off an uploaded page
could put entries into the provenance list** -- demonstrated, three ids
collected where one was real. That is the single property the agent exists to
guarantee, failing on the only input the system treats as untrusted.

The fix is a typed `ToolResult`: a payload for the model, and the document
ids stated separately by each tool. The walker is deleted. Adding a tool now
means deciding what its sources are rather than hoping they are found, and
`get_source` takes the id off the row rather than out of the payload.

**The same amount had three spellings.** `Numeric(14, 4)` reads back
`10.0000` where the mapper wrote `10.00`; the analyst quantised to two
places; the worker called `str()` and got whichever it held. So
`document_events` recorded `"9.35"` for a row holding `9.3500` -- the same
number, and an audit trail that did not obviously match the table it
described. That one was noticed during the ledger work and papered over with
a test comment, which was the wrong call.

The fix is a `Money` TypeDecorator over `Numeric`, normalising on the way out
of the database, plus one `core.money.to_str` that every caller uses. This is
the problem `UTCDateTime` already solved for datetimes in 5.9 -- a type that
does not round-trip cleanly -- and `render_item` in `alembic/env.py` was
written for exactly this case, its docstring naming a `core.models.Money`
that did not exist until now. No migration: the column is unchanged.

**What this says about the tests.** Both bugs were caught by tests written
alongside the feature -- but caught because those particular behaviours
happened to get a test. The citation bug surfaced only because drill-down
citations were tested specifically, and the injection variant was found later
still, by asking what else the walker would match. More tests is not the
defence. A dataclass with a `document_ids` field cannot be quietly wrong
about where its ids came from.

Not built: persistent chat. `ask()` keeps history inside one call, which is
the part that has to be right first -- one question producing one auditable
set of tool calls. Sessions are a thin layer on top and belong with the UI.

### 7.8 Positional hints: measured, dropped; row reconstruction: measured, built

The question came from a previous system that let an author describe a field by
position, e.g. "delivery date, top left; not the date top right". That works today
in the sense that `description` is free prose, so the hint reaches the model
verbatim inside the JSON Schema.

Whether the model *acts* on it was tested with four live calls. A delivery note had
two unlabelled dates in the header, and the hint named the left one. Each run used
flattened OCR text and hand-padded column layout, with both orderings:

```
flat text:    1/2 correct   (returned the earlier date both times)
padded text:  1/2 correct   (returned the later date both times)
```

It never used the position. Each rendering just shifted which heuristic the model
fell back on. Four calls on one synthetic document is suggestive rather than
settled, but it was enough to **stop a layout-reconstruction feature being built on
assumption**.

The explanation turned out to be architectural. The earlier system generated
*code* to parse each format, and code reading a PDF gets coordinates, so "top left"
is a perfectly good instruction to a programmer. Ledgerline's extractor reads
flattened text, where position is gone. `layout_blocks` with bounding boxes are
stored on every document but never sent to the model, a gap between §5.12's stated
intent and the build.

So positional logic belongs in generated parsers (phase 6) or in vision (phase 4),
not in text extraction. For text, the robust form is the label plus a contrast:
"the date next to *Fecha de entrega*, not the one next to *Fecha albarán*". The
contrastive half of a positional hint carries over; the "top left" half doesn't.

**Then real receipts settled the other half of the question.** That experiment asked
whether a model can *reason* about position, and it can't. A different question turned
out to matter more: whether the text keeps a printed line together. On a
two-column Italian till receipt (product left, price right), PP-OCR's reading order
listed the names and then the prices out of step. The model paired them by position
and got almost every item wrong while still getting the total right. That's why the
error was invisible from the headline number.

`ocr/layout.py` rebuilds printed lines from the boxes: fragments at the same height
are one line, ordered left to right, placed in proportional columns. Same receipt,
same model, same OCR:

```
flat text     19 items, amounts on the right item:  1/19   total 52,08
rebuilt rows  19 items, amounts on the right item: 19/19   total 52,08   (same cost)
```

That's the line items gap between §5.12's intent and the build, closed. It's built
because it was measured, not because it seemed sensible; the first layout idea, which
seemed equally sensible, failed its measurement and was dropped. On receipts where
name and price print close enough to be one OCR fragment, nothing changes, which is
the right outcome. `extraction_text` is the single place both the worker and the eval
harness get their text from, so the two cannot render a document differently.

**The first version regressed a tilted photo.** A receipt shot at −3.3° lifts the
right-hand column by a full line across the page, so grouping on raw heights attached
every price to the label one row below it, and `TOTALE` picked up the cash tendered.
Flat text had done better on that receipt. The label caught it, which is the point of
a fixed answer key.

The correction was already half-built. PP-OCR returns each detection as a
quadrilateral that keeps its rotation (`TextBlock.polygon`), where `bbox` discards it.
The median slope of the outlines is the page's tilt, and each fragment's height is
corrected by its distance across the page before grouping. Fragments narrower than
15px don't vote, since a two-character box tilts whole degrees from one pixel of
jitter. Pages without outlines keep the old behaviour. After the fix, both receipts
are right: 19/19 on the two-column one, 3/3 plus the total on the tilted one.

**A check that turned out not to check.** To look for further regressions without
finished labels, the 20 receipts were extracted both ways and tested on whether the
line items sum to the total. That catches misreads and missing items, but it **can't
see a mis-pairing at all**: a swap moves the same amounts between lines and the sum
is unchanged. The two-column receipt "added up" in both renderings while one of them
had 18 of 19 items wrong. Pairing is only measurable against labels.

What the check did surface was **OCR misreads**, a separate class this doesn't touch:
`4.00` read as `400`, `0.70` as `8.70`, `0.56` as `950`, `UOVO` as `LOVO`. Both renderings
carry the same misread digits; the model sometimes "repaired" them from flat text, which
the system prompt forbids, and copied them faithfully from rows. The label holds what's
printed and the scorer counts the difference. That's an end-to-end measurement, OCR
included, and it's the case the vision fallback exists for.

### 7.9 Seeing the spend, and the spend that was missing

Tokens, cost and latency had been recorded on every extraction since phase 2, and
nothing read them. `core/costs.py` does now, through the CLI and `GET /costs`, the
latter protected by the app-wide key check with no code in the route.

It keeps three numbers apart because they're easy to blur. **Total** is everything
billed. **Wasted** is the part spent on calls that produced nothing usable. **Per
document** is total divided by distinct documents, which is higher than the cost of
one call by exactly the retries. Authoring trials are billed too but aren't
production traffic, so they're reported beside the total. A call that used tokens
but priced at zero is flagged, not summed as free.

**Building it exposed a gap: billed failures left no record.** A refusal or a
`max_tokens` truncation is an HTTP 200 with a full usage block, so it's charged
exactly like a success. The extractor raised, the worker recorded nothing, and the
spend disappeared. A cost report built on top would have under-reported precisely
the calls that bought nothing, which are the ones it most needs to show.

Now `ExtractionError` carries a `spent` result whenever the failure happened after
a response arrived. `repository.record_failed_attempt` writes it as an extraction
with `is_current=False` and no `raw_json`, so it counts for cost and attempt
numbering but can never replace a document's real answer. A failure before any
response, like a dropped connection, carries nothing because nothing was billed.

A test also caught a bug in the latency percentiles. Nearest-rank is
`ceil(p × n)`, and `round()` had been used instead. Python rounds half to even, so
`round(2.5)` is `2` and the median of five values came out as the second-smallest.

Live, three receipts: $0.005872 total, $0.001957 per document, p50 2.1s.

### 7.10 First baseline: 20 real receipts

Twenty Italian till receipts from Wikimedia Commons (CC BY-SA, attributed in
`data/evals/SOURCES.md`, not committed), drafted with `--draft-labels --as receipt`,
then checked field by field against the images and verified by hand. Italian rather
than Spanish because only six Spanish receipts exist on Commons, and they're 1930s
donation slips; Italian shares IVA, TOTALE and the euro, so it routes to the shipped
type unchanged, while CONTANTE, *Pagamento elettronico* and P.IVA test whether the
descriptions generalise.

Scored through the live pipeline (PP-OCR, rebuilt rows, Haiku 4.5):

| | Routing | Field accuracy | Cost / document |
|---|---|---|---|
| What the pipeline delivers | **9 / 20 (45%)** | 91.1% on the 9 | $0.0011 |
| Extraction on all 20 (`--past-routing`) | — | **93.9%** | $0.0026 |

Total, currency, tax id and payment method: 100% on all 20. Weakest: vendor (70%) and
line item names (89%).

**The headline is routing, not extraction.** The pipeline returns nothing for 11 of
20 receipts. Small bars print TOTALE but no IVA line, and the other routing signals
are Spanish, so one keyword out of eight scores below the 0.2 threshold. A field
accuracy quoted alone would have hidden that, which is why routing is reported
separately and why `--past-routing` exists.

Every failure, by cause:

| Cause | Count | Fixed by cleaner images? |
|---|---|---|
| Routing | 11 receipts | No; the matchers are Spanish |
| OCR misreads (`0.70` → `8.70`, `FARMACO` → `FARHACO`) | ~9 | Yes |
| Vendor: which name counts (trading name vs full header) | 5 | No; a definition question |
| Quantity lines (`6 X 0,22`) missed or taken as items | ~5 | No; a description gap |
| The model calculating (`subtotal` 10.73 = 11.85 − 1.12) | 1 | No; and against the system prompt |

Cleaner receipts would fix about a third of the field errors and none of the routing.
Born-digital PDFs are a stronger lever than scan quality: the OCR container currently
rasterises every PDF, even one whose text layer is already exact.

**Labelling had its own error rate.** Three "model errors" in the first scoring were
label errors, OCR misreads carried over from the draft (`BOR` for BAR, `GHIACC10` for
GHIACCIO). Correcting a draft tends to keep its typos, because a reviewer checks values
rather than retyping them.

**Runs vary.** The same 20 labels scored 94.6% and 93.9% on consecutive runs; on one
receipt the model wrote the correct `GHIACCIO` once and copied the OCR misread the next
time. At n = 20 a single run moves by about a point. These numbers are honest with an
error bar, not precise, and a claimed improvement smaller than that isn't one.

### 7.11 Born-digital PDFs skip OCR

The project's focus is moving to English invoices, which mostly arrive as PDFs
exported from accounting software. Those PDFs already contain their text, exactly,
with every character's position. The OCR container nevertheless rasterised every
PDF and recognised it back, spending seconds to replace exact text with a guess at
it.

`ocr/pdftext.py` reads the text layer first. Fragments come from PDFium's text
rectangles, which are runs on one line, so on a two-column invoice the description
and the amount arrive separately with exact boxes, the input row reconstruction
needs. Coordinates are converted to the top-left origin every other engine uses.
A PDF with fewer than 20 non-space characters (a scan, an image-only export) falls
back to OCR, as does a malformed one.

It was a wrapper, `TextLayerFirst`, applied inside `get_engine`, so the worker and
the eval harness take the same decision and can't disagree about which documents were
OCRed. That put PDF parsing in the worker, which §8.9 records and step 2 fixed: the OCR
container now runs the same `read_text_layer` itself, and the wrapper is left only on
the in-process test double. `ocr_results.engine` records which reader actually ran:
`pdf-text` or `ppocr-onnx`.

Measured on a generated two-column invoice: **18 ms against 1.7 s** for the OCR
container, the same 18 fragments, and correct rows from both. That PDF is clean vector
text, the easiest case OCR gets, so the measured win is speed, no container, and a
guarantee. The accuracy win is where OCR misreads: small type, currency symbols, long
reference numbers. On the Italian receipts those misreads were about a third of all
field errors, and for a PDF with a text layer they can't occur. That claim is
structural, not yet measured on real invoices.

Two limits. `mean_confidence` is left null rather than 1.0, because a scanner's own
OCR layer looks identical from here and isn't exact. And the decision is per
document: a PDF with some text pages and some scanned ones uses the text layer
throughout.

### 7.12 A required field forced the model to invent

The first English invoice corpus is 20 synthetic invoices from FATURA (CC BY 4.0,
one per template across 20 layouts). Several of those templates print no total at
all. On them, the drafts came back with totals that appear nowhere on the page:
1492.80, the line items summed; −460.50 and −1012.20, sums carrying a stray minus
taken from a discount's `(-)`.

The cause was in the type, not the model. `total` was declared `required: true`, and
a required field is **non-nullable in the JSON schema**. Structured outputs then
guarantee a number, so on a document with no total the model can't say so, and it
computes one. The schema overrode the system prompt's own rule, *a null is correct
and useful; a guess is not*.

Absence was already somebody else's job. Both types carry a `required` *rule*, which
fails validation with "total is required but was not extracted" and sends the document
to review. So the field is now nullable and the rule does the enforcing: the no-total
invoice returns `null` and goes to review visibly, and an invoice that prints its total
still gets it. A test now fails if any shipped type marks a money field `required`.

It was caught by a check worth keeping: **an amount the model returned that appears
nowhere in the OCR text was computed, not read.** That check found four across the
corpus, two of them on labels already verified by hand. An invented total that's
arithmetically consistent with the line items looks exactly right on review, so
plausibility is no defence; only the page is.

The pattern is now three instances, each a different route to the same failure: a
subtracted subtotal on an Italian receipt, two taxes summed into one `tax_amount`,
and these totals. The descriptions say "never compute"; the model computes anyway
when a field invites a value the page doesn't hold.

### 7.13 Several taxes: the model reads them, code adds them

An invoice can print GST and VAT as separate lines with no combined tax figure. A
single `tax_amount` field left two bad options: compute the sum, which the model did
unprompted (105.79 = 89.03 + 16.76), or record null and lose the tax entirely.

The invoice type now has a `taxes` list, one entry per printed tax line with its
name, rate and amount, all copied rather than computed. `tax_amount` is reserved for
a total that's actually printed. A new `canonical.tax_lines` lets the ledger sum the
list **in code** when no total is printed, so the ledger still has the right tax
and the model never adds anything up. If any line's amount is unreadable, the sum is
null rather than smaller. Tax shown only as a per-item column, which appears on two
FATURA templates, isn't captured yet.

Adding the list exposed a hard API limit: **structured outputs accept at most 16
union-typed parameters per schema**, and every optional field is a nullable union.
The invoice reached 18 and the API rejected every request, so no invoice would have
been extracted at all. The cap is now checked when a definition is built, with a
message saying how to fix it, so a type authored through the UI can't pass
validation and then fail on every document. Inside the list, a tax line's name and
amount are required, because a line that exists always prints both; only `rate` is
optional. That puts the invoice at exactly 16: it can't gain another optional field
without making one required or splitting the type.

Two seeding bugs surfaced along the way. Adding an optional field to the definition
*format* made every stored type look changed, because stored JSON lacked the new
default, and versioned the unchanged receipt type. Definitions are now compared
after both pass through the model. And a stricter rule can make a stored definition
invalid: the registry skips it, so the type stops routing, and the seed must replace
it rather than crash trying to compare against it.

### 7.14 Invoices on a held-out set

The 20-invoice corpus was used for tuning: the nullable total, the `taxes` list and the
lone-tax rule were all changes made because of what those invoices showed. Its 99.5%
is therefore a training score. The honest number comes from 15 more invoices, one from
each of 15 FATURA templates the tuning corpus never used, drafted with the invoice type
frozen at v5, verified by hand, and scored once:

| | Routing | Field accuracy | Cost / document |
|---|---|---|---|
| Held out, 15 unseen layouts | **12 / 15 (80%)** | **100%** (`--past-routing`) | $0.0038 |

Three qualifications travel with that 100%. Fifteen documents can't distinguish it from
about 95%; one error would have read 93%. FATURA is synthetic and cleanly rendered, the
easiest possible input. And the labels began as drafts from the same model, so an error
that is plausible on review would score as correct. What it does show is no sign that
extraction overfits to the layouts it was tuned on.

**Routing failed on 3 of 15, and two were claimed by `receipt`.** That uncovered two bugs
in `match_score` that the tuning set never exercised:

1. **Keywords match as substrings.** The receipt keyword `iva` matched *Sullivan* in a
   customer's name and *rivassarah* in an email address. (The Italian receipts had
   routed partly by the same accident: `total` inside *TOTALE*.)
2. **Scores aren't comparable across types.** A score is hits over signals, so two hits
   score 0.222 for the invoice type's nine signals and 0.25 for the receipt type's
   eight. A type with more keywords loses ties for being thorough.

The third miss is ordinary: "invoice" was the only invoice word on the page, 1 of 9
signals.

Per the holdout rule, these are recorded here before being fixed. Once routing changes
in response to this set, **its routing number is spent**: a routing claim afterwards
needs fresh templates (15 FATURA templates remain unused). The extraction number stays
valid for as long as extraction isn't changed because of it.

**Fixed afterwards.** Keywords now match whole words, where only a letter counts as
part of a word, since OCR glues digits on (*IVA21%*). Types are now ranked by how many of
their signals hit, and the share breaks ties; the share still decides whether a type
matches at all. Whole words alone would have cost all nine Italian receipts that routed,
so the receipt type gained `totale`. Routing-only, on cached OCR text:

| | Before | After |
|---|---|---|
| Receipts (20) | 9 | 9 |
| Invoices, tuning (20) | 17 | 17 |
| Invoices, holdout (15) | 12 | 14 *(spent: not a claim)* |

The remaining invoice misses (t06, t16, t27 and holdout t20) print "invoice" and no other
invoice keyword. One hit is not enough by design, so those need either more keywords or
the model-classifier fallback, not a looser threshold.

---

## 8. Known gaps and deferred work

Stated plainly. These are known, not overlooked.

**8.1 ~~No tests~~ — resolved.** 128 tests, 1,488 lines, running in **0.8s** with no
container and no tesseract required.

| File | Tests | Covers |
|---|---|---|
| `test_storage.py` | 27 | hashing, sharding, idempotent writes, magic-byte sniffing |
| `test_ocr.py` | 30 | protocol conformance, TSV parsing, HTTP error mapping |
| `test_api.py` | 22 | every endpoint, every validation and 4xx path |
| `test_worker.py` | 22 | claim atomicity, backoff, stale reclamation |
| `test_repository.py` | 13 | ingest transaction, dedup, the unique-index race |
| `test_models.py` | 9 | `UTCDateTime` round-tripping (the bug in 5.9) |
| `test_pipeline.py` | 5 | end to end: upload → worker → read back |

Three things made this possible, and each was a design decision made earlier:

- **`core/db.py` was refactored to a `create_db_engine(url)` factory.** Tests build an
  engine against a tmp_path database and get the same pragmas as production —
  foreign key enforcement in particular, which SQLite disables by default and which
  would otherwise let tests pass on constraints that do not hold.
- **`OcrEngine` being a Protocol means the worker is testable with a `FakeEngine`.** No
  container, no binary, no network. The swappability argument from 5.12 paid off as
  testability.
- **Routes take their session by injection**, so `app.dependency_overrides[get_session]`
  redirects the whole API at a sandbox database.

The suite immediately earned its keep by surfacing a deprecation
(`HTTP_413_REQUEST_ENTITY_TOO_LARGE` → `HTTP_413_CONTENT_TOO_LARGE`), now fixed.

It also established something the code did not say: the `document is None` guard in
`run_ocr_job` is unreachable while foreign keys are enforced. Constructing that orphan
in a test requires `PRAGMA foreign_keys=OFF`. The branch is kept — SQLite disables
foreign keys by default, so any database created without the engine factory would allow
exactly that — and the test now documents why.

Still uncovered: `main()`'s polling loop, and the OCR container's own `app.py` (tested
by hand via the endpoint, not by pytest). Phase 2 added 219 tests of its own — see 7.5 —
taking the suite to **347 in 1.7s**, still with no container, binary, network or API key
required.

**8.2 ~~`/health` is liveness only~~ — resolved in step 2.** `/ready` compares the
database's `alembic_version` with the newest migration shipped beside the code, and
answers 503 with the reason until they match. The compose API's healthcheck uses it, and
the worker waits on the same check before taking a job. `/health` stays liveness:
something to restart on, not to route on. What follows is the gap as it was found.

`/health` runs `SELECT 1`, which succeeds against any
openable database — including one with zero tables. Demonstrated by rolling the migration
back to `base` and observing `200 {"status":"ok","db":"ok"}` against an empty schema. The
failure mode it cannot catch: deploy new code, forget to migrate, container reports
healthy, every real request 500s. The fix is to compare `alembic_version` against the
head revision the code expects. Deferred deliberately — it becomes load-bearing at phase
5, when schema bumps drive `reprocessing`.

**8.3 ~~`DocStatus.DUPLICATE` is unused~~ — resolved by dropping it.** Because `sha256`
is `UNIQUE`, a repeat upload cannot create a second row, so no document could ever hold
the status. The alternative was to drop the unique constraint and let repeats create
rows marked `duplicate`, which trades a structural guarantee for a status nobody reads.

A duplicate upload is an *event*, not a state: the API returns the original with
`duplicate: true`, and what happened is already in `document_events`. The status is gone
from `DocStatus` and from the README state machine, which no longer promises a state the
schema cannot produce.

**8.4 ~~No PDF support~~ — resolved, and digital PDFs skip OCR (7.11).** The OCR container rasterises PDFs with
`pypdfium2` at 200 DPI, capped at 20 pages. Tesseract still refuses PDFs by design, since
it has no rasteriser and should not guess; that path is now only used for tests.

**8.5 ~~No Docker Compose~~ — resolved in step 2.** Two profiles. `dev` starts Postgres
and the OCR container for the `uv run` workflow. `app` runs everything from one image:
a `migrate` step (migrations, then the create-only seed) that the API and worker wait
on, then both. See §9.2.

**8.6 Content-level deduplication is unsolved.** `sha256` catches "you sent me this exact
file again." It does not catch "this is the same receipt." A user who photographs a
receipt, is unsure it uploaded, and photographs it again produces two different images of
one purchase — two OCR runs, two extractions, and eventually **two transactions for one
lunch**. Catching that requires comparison after extraction (same vendor, total, date,
tax ID → probable duplicate → review queue). Phase 4, and a genuinely different mechanism
from what exists.

**8.7 ~~Cost tracking is unexercised~~ — resolved, and now visible.** Every extraction
row carries tokens, cost and latency, and `python -m core.costs` / `GET /costs` read them
back: total, wasted, per document, by model and type, latency percentiles. See 7.9.
Still not covered: the analyst agent's calls, which are reported on each `Answer` but not
persisted. `runs`/`steps` for multi-call traces are roadmap step 6.

**8.8 Security posture is incomplete, but no longer open.** Uploads are size-capped and
type-checked, paths are derived from content hashes rather than user-supplied filenames
(so path traversal is structurally impossible), and the API now requires a key — see
5.22. Still not addressed: encryption at rest, PII redaction before analyst context, and
rate limiting. And the key buys authentication only: nothing in the schema has an owner,
so any caller that gets in sees everything.

**8.9 ~~Untrusted PDFs are parsed outside the OCR container~~ — resolved in step 2.**
The OCR container now reads the text layer itself, as the first step of `/ocr` for a PDF.
It runs the same `ocr/pdftext.py`, copied into the image rather than re-written, so the
two paths can't disagree. The production engine no longer touches a PDF in-process, and
a test makes any in-process parse fail. Building the image from the repo root needed a
`.dockerignore`, which keeps `.env` and `data/` out of every build context. What follows
is the gap as it was found.

The argument for the
container (§5.16) is that files from strangers are parsed only where there are no
credentials and no network. Reading the PDF text layer (§7.11) broke that quietly. It
runs PDFium in the worker, which holds the database credentials, and since the panel's
trial uploads, in the web process too. A malicious PDF exploiting a PDFium bug would run
beside the database password. Mitigated so far only by the 20-page limit now applied
in-process too. The fix is to read the text layer inside the container, as a first step
of its `/ocr` for PDFs, and that lands with step 2's compose work.

---

## 9. Roadmap

Reordered on 2026-10-23, once phases 1–3 and 7 had shipped. The old phase numbers are
kept in brackets, so earlier sections that say "phase 6" still point somewhere.

**Done**

| Phase | Shipped |
|---|---|
| 1 | Upload → dedup → OCR → stored text; status tracking; event log |
| 2 | Processors as data: routing, schema-enforced extraction, validation, ledger mapping; transforms (§5.23) |
| 3 | Eval harness: per-field accuracy, cost per document, a held-out set (§7.14) |
| 4, in part | The `invoice` type; born-digital PDFs read from their text layer (§7.11) |
| 7 | Analyst agent over `transactions`: read-only tools, citations, approval gate |

**Next, in order**

| Step | Ships | Done when | Demonstrates |
|---|---|---|---|
| **1** (phase 6a) | Processor authoring: API, panel, user accounts, trial runs, versions, publish and retire (§9.1) | A new layout gets a published processor without anyone touching YAML | Self-serve document types; auth for people |
| **2** | Deployable: Postgres, compose for every service, a readiness check, a hosted demo on synthetic data (§9.2) | A public URL, and the suite passing on Postgres in CI | Production shape, early |
| 3 (phase 4) | Review queue and corrections; corrections become eval labels; probable-duplicate detection (§8.6); reprocessing after a publish | No document sits in review without a way to fix it | Human in the loop; labels from use |
| 4 (phase 4) | A model classifier when no keyword matches; vision fallback for photos that fail their rules; each tier measured | A table of share, accuracy and cost per tier | Cost engineering |
| 5 (phases 5, 6b) | A template agent that drafts processors through the authoring API, scored on a fresh holdout. LangGraph only if the draft → trial → revise loop needs it | An agent-drafted processor reaches a stated accuracy on unseen layouts and is published after review | Agents, measured; orchestration where justified |
| 6 (phase 8) | Outbox with CSV export, then an accounting API sandbox; per-document traces; rate limiting; PII redaction before analyst context | Exports retry safely; every model call on a document is visible | Production shape |
| Stretch (phase 6) | Generated deterministic parsers for recurring born-digital vendors, in a real sandbox | | Removing the model from the hot path |

**Why this order.**

- **Authoring comes first** because steps 3 and 5 reuse its screens, users and API.
- **Deploying comes second** because moving from SQLite to Postgres gets riskier the more
  is built on SQLite, and a live demo is worth more early than late.
- **Review comes before cost tiers** because the queue shows what is actually failing,
  and corrections supply fresh labels to measure the tiers against.
- **LangGraph changed role.** It was promised as phase 5, for batch fan-out and
  interrupt/resume at review. The `jobs` table already does fan-out, and review is a
  status rather than a paused process. So LangGraph is now tied to the one genuinely
  multi-step workflow, the template agent's loop, and adopted only if it measures better
  than a plain loop (§5.19).

### 9.1 The processor panel

The panel is for the people who write processors, so they don't have to edit YAML and
re-run the seed. Decided:

- **API first, panel second.** Plain functions in `doctypes/authoring.py` do the work:
  draft, publish, retire, trial-run. The JSON API, the panel and the phase-6 drafting
  agent all call them, so validation lives in one place: `Definition`, the nullable cap
  and the transform examples. The API's shape can change without touching the rules.
- **Server-rendered: FastAPI with Jinja2 and HTMX.** The pages run in the same process
  and call the same functions. There is no JavaScript build: CodeMirror for the YAML and
  Python editors comes from a CDN. What you edit is exactly the seed format, so nothing
  maps a form onto YAML. A single screen moves to a richer frontend if it ever outgrows
  this. Rejected:
  - **Django admin**: a second ORM over tables SQLAlchemy and Alembic own, and edit and
    delete buttons that do the wrong thing here.
  - **sqladmin**: one JSON text box for the whole definition, and it writes to the
    database around the rules.
  - **A React app**: a second language and build chain for a handful of trusted authors.
- **Edit means a new version, delete means retire.** `document_types` is append-only
  because extractions cite the version that produced them, and a real delete would
  orphan them. Retiring deactivates the type: it stops routing and keeps its history.
- **Every version records what it was based on.** `based_on_version` and `created_by`.
  Publishing refuses if the base is no longer the active version, and shows what changed
  instead. This is optimistic concurrency against lost updates: two tabs, two people, or
  the drafting agent writing while someone edits.
- **Seeds create, never update, once the panel ships.** From then on the panel is the
  only way to change an existing processor, so a deploy can never quietly revert an
  edit. `doctypes/seeds/` remains the starter set for a fresh database (laptops, CI,
  tests). It is not production's copy, and it will fall behind; an export button can
  come when that matters.
- **It needs real users.** The panel saves Python the worker runs, which makes editing a
  code-execution privilege. The static machine key (§5.22) stays for machines. People
  get accounts, password hashes and a session, with two roles: *author* (drafts and
  trial runs) and *admin* (publishing, which puts code live). This is the trigger §5.22
  named for revisiting auth.

**Built so far:** the functions and the JSON API (`api/routes/processors.py`).
Publishing locks the type's rows before the version check, so two racing publishes
serialise on Postgres instead of both passing. A trial on loose text or an uploaded
sample is returned and not stored: ingesting a sample would deduplicate the real copy
away when it later arrives. Checked live against the real model: a draft of `invoice`
tried on text with no total line came back with the total null, the transform derived
it as 107.99, and publishing a stale draft returned 409 naming v3.

Then accounts and sign-in (`core/users.py`, `/auth/login`, `/auth/logout`, `/auth/me`),
with roles enforced on the processor routes: drafting and trials need *author*,
publishing and retiring *admin*. See the end of §5.22.

Then the first pages (`panel/`): sign-in, the processor list, version history, each
version shown as seed YAML, and a diff between any two versions. Defaults are left out
of the YAML, so a field reads as its type and description; a transform's code comes
out as a `|` block, so it reads and diffs as code. The browser-specific parts:

- **No session means a redirect to sign-in, not JSON**, and only under `/panel`. An
  HTMX request whose session ran out gets `HX-Redirect` rather than a blank swap.
- **Signing in is a plain form post**, so it works before any script loads. It is the
  one state-changing page route without a CSRF token: there is no session yet to ride.
  Everything else that changes state goes through HTMX, which sends the token from
  the page in a header.
- **A strict Content Security Policy on every page**: scripts from this origin only, no
  inline script or style, no framing. HTMX 2.0.4 is vendored, checked byte for byte
  against the npm package's published hash, rather than loaded from a CDN. Jinja2
  escapes everything authors write, and a test puts `<script>` in a display name to
  prove it. The CSP is the second layer: injected script would not run even if
  escaping failed.

Then editing and trial runs. A published version offers "start a draft from vN"; a draft
opens in an editor with **two boxes, the YAML and the transform's Python**, because
Python inside a YAML block breaks on one wrong indent. They are joined back into one
definition on save, and a `.py` upload can replace the code box. Saving never runs the
examples: a draft may be work in progress, and the examples gate publishing instead.
A failed save comes back as a 422 fragment listing every problem with its path, and the
page's HTMX config swaps 403, 409 and 422 in beside the form instead of dropping them.

The trial page takes a sample file, a stored document's id or pasted text, and shows:
- the routing table, ranked the way routing ranks;
- every field, with the derived ones marked;
- the rule errors;
- the model's raw answer, before the transform;
- cost and latency.

**Not done yet:** the boxes are plain text areas, so Tab moves focus instead of
indenting. A code editor (CodeMirror) is the obvious next layer, vendored like HTMX.

Then publishing and retiring, admin only, each behind a confirmation page. Before
publishing, the page shows what `publish` would check, run the same way
(`authoring.publish_preview`):
- the diff against the live version;
- a warning when the transform's code changes, because new Python will run on the
  server;
- the result of every example;
- whether someone published since the draft was started.

If any check fails, there is no button. Confirming runs every check again, because the
page may be minutes old, and a conflict then comes back as a 409 beside the button.
Retiring says what stays: every version and every extraction made with it.

**The seed switch is done.** `doctypes.seed` now only creates processors that don't
exist. It reports, and leaves alone:
- one whose seed file differs from what is stored;
- one that is retired;
- one that only has drafts in the panel.

`--replace NAME` is the break-glass. It publishes the seed over what is stored, for the
one case the panel can't handle: a stored version that no longer validates, so it
doesn't route and can't be opened to start a draft.

**Then Draft with AI** (`doctypes/drafting.py`), the first slice of the template agent.
An author describes the document and what they need, optionally with a sample, and a
stronger model tier (`claude-sonnet-5`, `DRAFTING_MODEL`) drafts the processor. What it is
trusted with:

- **It drafts data, never code.** Fields, descriptions, keywords, `required` checks and
  the ledger mapping all go through the same validation as a person's YAML. It never
  writes a transform, because transforms are code the server runs, and people write
  them (§5.23). Needed arithmetic goes in its notes for a person to write.
- **Two lessons are enforced in code, whatever the model answers.** Top-level fields are
  always nullable, and "must be present" becomes a `required` rule (§7.12: a forced
  amount gets invented). The minimum routing score is the seeds' 0.2.
- **One repair, then a person.** A draft that fails validation, or takes a name already
  in use, goes back to the model with the exact problems, once. If it fails again,
  nothing is saved and the author gets the YAML with the problems listed, to finish by
  hand.
- **It lands as an unpublished draft**, marked `(ai draft)` in `created_by`, behind the
  same trial runs and publish checks as anything else.

The model answers in a schema built for drafting rather than `Definition`'s own. That one
keys fields by name, and a structured-output schema can't have dynamic keys. It also has
no nullable values, so it never nears the 16-union limit itself.

Checked live: described as purchase orders and given an invoice as the sample, the draft
validated first time, for $0.025. Its descriptions were label-based and ended "Null if
not printed". It noticed the sample was an invoice, left out the ledger mapping because a
purchase order is not a payment, and flagged that a missing line amount would need a
transform. Drafting calls are not yet in the cost report, because there is no document
to attach them to, like the analyst's (§8.7).

### 9.2 Why Postgres

SQLite was right for getting started: nothing to run, and a suite of several hundred
tests in seconds. It isn't slow, and for a single-user tool on one machine it would
still be the right choice. The roadmap changes the conditions:

- **Several writers on several machines.** SQLite allows one writer at a time, on one
  disk. WAL mode and a five-second busy timeout cover one API and one worker. More
  workers, several panel users, the drafting agent and reprocessing batches don't fit.
  A worker on a second machine can't share the file, and hosting pins it to one
  instance.
- **The queue.** `SELECT … FOR UPDATE SKIP LOCKED` lets many workers claim different jobs
  at once. On SQLite, claims take turns.
- **Money, measured.** SQLite has no exact decimal type: `NUMERIC` stores money as a binary
  float, so SQL sums are float sums. `SELECT sum(a)` over 1.10 and 2.20 returns
  `3.3000000000000003`. The analyst's per-vendor totals are summed in SQL, and they come
  back right only because SQLAlchemy rounds to the column's four places. That is correct
  by rounding, against the rule that money is never a float. Postgres `NUMERIC` is exact.
- **The database enforces what we currently enforce by hand.** Foreign keys are off by
  default, so `core/db.py` turns them on at every connection. There's no timezone-aware
  timestamp, hence `UTCDateTime` and the bug in §5.9. Columns can't be altered in place,
  hence batch-mode migrations. SQLite also accepts a string in an integer column.
- **A read-only database user for the analyst.** Its tools only read. With Postgres, its
  connection can only read too, so one bug or prompt injection can't write.

The cost is one more service (a managed free tier is enough for the demo), and a CI job
running the suite on Postgres so the two dialects can't drift apart unnoticed.

**Prepared before the switch.** A review of the whole codebase ahead of step 2 found
these and fixed them:

- **Three rules the ledger rests on were held only by application code.** They were one
  current extraction per document, one current ledger row per document, and one active
  version per processor. They are now partial unique indexes, which SQLite and Postgres
  both support. Two workers racing a demote-then-insert get an integrity error instead
  of a receipt counted twice. Tests write the racing row directly and check it is
  refused.
- **Uploads shared one temp file name.** Two simultaneous uploads of the same bytes, a
  client retrying against several API workers, could rename a half-written file into
  place, or crash the slower writer. Each write now has its own temp file. The test
  fails on the old code three runs in three.
- The worker built a new model client, and new connections, for every job. It now
  builds one.

**Then the switch itself (step 2.1).** Postgres 17 runs in compose for development. The
whole suite runs against it with `TEST_DATABASE_URL` set: the schema is built once and
every table truncated between tests. Running it found four things SQLite had hidden:

- **Timestamps were half naive.** The first migration was written by hand with
  timezone-aware columns. Every later one was generated from `UTCDateTime`, whose impl
  was a plain `DateTime`, so 14 Postgres columns were `timestamp without time zone`.
  Postgres converts an aware value into those using the server's `TimeZone` setting,
  so what got stored depended on configuration. `alembic check` had missed it, because
  it compared the TypeDecorator itself with the column; it now compares the impl. A
  Postgres-only migration converts those columns, reading the old values as UTC.
- **`JSONB` sorts object keys.** Extraction data became `JSONB` for indexing and
  querying by key. A processor's `definition` stays plain `JSON`, because its field
  order is the author's: it's the order the model sees fields, the panel lists them
  and a draft label reads. A test caught `JSONB` reordering them.
- **Column lengths are enforced.** An uploader's 300-character filename was a 500 on
  Postgres (`value too long for type character varying(255)`), and SQLite had stored it
  whole. Fixed:
  - filenames are cut, keeping the extension;
  - the worker id fits `locked_by`, since a long hostname would have failed every
    claim;
  - display names and profile names are validated when a processor is defined;
  - `created_by` widened to fit the longest legal email plus `(ai draft)`.

  `tests/test_column_limits.py` sends the longest realistic value down each path.
- **The claim takes row locks.** On Postgres it is `SELECT … FOR UPDATE SKIP LOCKED`
  then the UPDATE, in one transaction, so ten workers polling at once take ten different
  jobs. The engine also checks pooled connections on checkout (`pool_pre_ping`),
  because a worker keeps its pool for days and the database drops idle connections.

**Then all of it in containers (step 2.3).** One app image serves the API, the worker
and a one-off `migrate` step. It is two-stage, so the runtime has the locked
dependencies but no installer (414 MB, down from 636). It runs as a non-root user, and
the root `.dockerignore` keeps `.env` and `data/` out of it. Compose starts Postgres,
then `migrate`, and only once that succeeds the API (whose healthcheck is `/ready`) and
the worker (which also waits on the schema itself).

The OCR container went from "holds no credentials" in a comment to isolated by
construction, on a Docker network marked `internal`. Checked from inside it:
- `db` doesn't resolve;
- the database's IP is "network unreachable";
- the internet doesn't resolve;
- the worker still reaches it.

Two qualifications. Publishing a port needs a network with a way out, so the `dev`
profile's OCR, which `uv run` reaches on localhost, is not isolated: it is the
development shape only. And the API is on the OCR network, because panel trial uploads
call OCR. A compromised OCR container could therefore reach the API, but only its
public routes (health, readiness, sign-in, which locks after ten failures).

Checked end to end in the containers: a born-digital invoice uploaded to the API was
read by the isolated OCR from its text layer, routed to `invoice` and extracted for
$0.0034. Its missing total was derived by the transform as 107.99, and it was committed
with `derived: ["amount"]`.

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

Points worth being able to defend, and where the reasoning lives. For how the pieces
connect, start with [WALKTHROUGH.md](WALKTHROUGH.md).

### Architecture

- **Why not put a model in every step?** §2, §5.12, §5.16. There is one model call in
  the pipeline, extraction. Routing, validation and mapping are code that can be
  tested.
- **Why is a document type a database row rather than a class?** §5.20. It lets an
  analyst add a type through a UI without shipping Python. The price is a closed rule
  vocabulary and JSON instead of per-type columns.
- **Why a database queue instead of Redis?** §5.3. Claims are transactional, and it's
  one less service to run.
- **Why are OCR and extraction separate jobs?** OCR is the expensive half. A failed
  model call shouldn't cost the document its recognised text.

### Correctness

- **How do you avoid double-processing?** §5.4, §5.7, §5.13. Content-addressed
  storage, a unique sha256, and the UPDATE is the lock.
- **What happens when a worker dies mid-job?** §5.15.
- **How do you handle money?** `Decimal` everywhere, `"1,234"` is rejected rather than
  read as 1.234, and one canonical spelling enforced at the database boundary
  (`core/money.py`).
- **What does a document cost, and how do you know?** §7.9. The total includes failed
  attempts, because a refusal is billed like a success. Recording those was a gap that
  only building the report exposed.
- **How do you stop a re-run receipt being counted twice?** One live ledger row per
  document, with `is_current`. §5.21.

### Measurement

- **How accurate is it?** *Not yet known, and the docs say so.* The harness is built
  (§7.6) and scores per field, separating *missed* from *hallucinated*. The labelled
  corpus is what's missing. Saying this plainly is better than quoting 97.7% from three
  synthetic receipts.
- **How do you know a change helped?** `--compare` scores two models on one corpus and
  lists regressions before the headline number. Sonnet cost 3× and moved nothing, and
  it made the *same* hallucination, which points at the prompt rather than the tier.
- **How do you stop the model grading its own homework?** Drafted labels are
  `verified: false`, and the runner refuses them. The Tesseract draft in §7.6 shows why:
  three fields were wrong and every one looked plausible.

### The agent

- **Why doesn't the analyst do arithmetic?** §7.7. A model summing from context is
  occasionally wrong in a way nobody can see, so the sums live in tested SQL.
- **How is it kept from writing?** No tool writes, so it's a missing capability rather
  than a rule the model is asked to follow.
- **Can its citations be trusted?** They come from typed tool results, never from its
  prose. An earlier draft read them out of payloads, which let extracted document
  content inject ids. §7.7.

### Security

- **Why a static API key and not a JWT?** §5.22. A JWT carries claims about who you
  are, and the schema has no "who". The consumer is a machine, revocation has to be
  instant, and JWT's stateless validation needs a denylist to revoke, which puts the
  database lookup back. The trigger to revisit is adding users or tenants.
- **Why SHA-256 and not bcrypt?** Slow hashes defend low-entropy human passwords. A
  256-bit random key has nothing to brute-force.
- **What doesn't it do?** It's authentication, not authorisation. Any key that gets in
  sees everything. Raise this before they do.
- **Why sessions for the panel, and not JWTs?** §5.22. Revocation is the requirement:
  signing out, disabling someone or changing a password has to take effect now. A JWT
  can't be revoked without a denylist, and a denylist puts the database lookup back.
  Server-side sessions give that for one indexed query.
- **You let a model write processors?** §9.1. It drafts data, never code: fields,
  descriptions and keywords, checked by the same validation as a person's work. The
  lessons that matter are enforced in code rather than asked for. It gets one repair
  attempt with the exact problems, and the result is an unpublished draft a person
  trial-runs and publishes. The transform, the one part that runs code, stays human.
- **You store Python in the database and run it?** §5.23. People write it, not the model,
  so it gets containment rather than a sandbox: a separate interpreter, stdlib only, no
  environment, a time limit. Examples run before a version is stored, and everything it
  changes is marked derived. A generated transform would change that answer, and the
  doc says how.

### Honesty

- **What went wrong and what did you do about it?** §5.9, a silent datetime bug caught
  before it shipped. §6.2, discarding unverifiable work instead of building on it.
  §7.7, two bugs fixed at the contract rather than the instance, plus the admission
  that more tests weren't the defence.
- **What did you decide *not* to build, and how?** §7.8. Positional hints failed a
  four-call experiment and were dropped. Row reconstruction was then built because a
  real receipt measured 1/19 → 19/19 line items correct. The same discipline, applied
  twice with opposite results. Four live
  calls showed that "top left corner" hints do nothing against flattened OCR text,
  which stopped a layout-reconstruction feature from being built on assumption.
- **What is still broken?** §8, all of it.

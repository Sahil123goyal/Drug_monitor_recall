# Drug Recall Monitor

Hackathon project for a synthetic household recall monitor. Python 3.11+,
FastAPI, SQLAlchemy 2, PostgreSQL 16, LangGraph, Groq, Streamlit, and LangSmith.

## Structure

```text
app/
  main.py             FastAPI entry point and durable worker lifecycle
  api/                Async inventory, run, approval, and evaluation routes
  agents/             LangGraph workflow, PostgreSQL runtime, audit repository
  core/config.py      Environment settings
  db/                 Declarative base, engine, and sessions
  models/             Six audit/inventory models, household runs, and enums
  schemas/            Pydantic Medicine, Notice, Decision, and extraction models
  sources/            SourceAdapter interface and OpenFDAAdapter
  services/           Matching, extraction, verification, persistence, run worker
fetch.py              Fetch and save openFDA candidates from the command line
run_agent.py          Start/resume/inspect a single-medicine graph thread
run_api.py            API launcher, including Windows async driver support
frontend/app.py        Five-page Streamlit UI (inventory, monitor, approval, results, tests)
scripts/init_db.py     Repeatable database bootstrap
data/synthetic/       Invented household fixtures; no real household data
tests/                Matcher, adapter, extraction, graph, API, and PostgreSQL checks
docs/                 Agent and API usage examples
.github/workflows/    CI checks with a PostgreSQL service
docker-compose.yml    Local PostgreSQL with persistent storage
.env.example          Configuration template
requirements.txt      Runtime and test dependencies
```

## Local setup (PowerShell)

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
docker compose up -d --wait postgres
.venv\Scripts\python -m scripts.init_db
.venv\Scripts\python run_api.py
```

Copy the environment template only on first setup. Keep `DATABASE_URL` in sync
with the Compose credentials and port. Use a URL-encoded password in that URL.
The init script creates missing tables and enum types and is safe to rerun. It
does not migrate existing columns; introduce Alembic migrations when the schema
starts changing. Database creation itself is handled by the Postgres container.

Start the UI in another terminal with
`.venv\Scripts\python -m streamlit run frontend/app.py`.
No API keys are needed to initialize the database or run deterministic matching.
The Streamlit UI, API, durable workflow, approval, mock sending, and verification
are implemented. Open http://127.0.0.1:8000/docs
for interactive endpoint documentation. See [API examples](docs/api.md) and
[agent CLI and tracing](docs/agent.md). The UI runs at http://127.0.0.1:8501;
see [UI setup and page guide](docs/ui.md).

## Fetch notices

After configuring `.env`, starting Postgres, and running `scripts.init_db`:

```powershell
.venv\Scripts\Activate.ps1
python fetch.py "ibuprofen"
# Without activation:
.venv\Scripts\python fetch.py "ibuprofen"
```

The CLI prints a JSON object containing `count` and `notices`. Full raw source
records are saved in PostgreSQL but omitted from the console. A successful empty
search prints `{"count": 0, "notices": []}`. Configuration, HTTP, and database
failures exit with code 1. Search hits are candidates, not exact-match verdicts.

`SourceAdapter.fetch_notices(query) -> list[Notice]` uses the Pydantic model in
`app.schemas.notice`, separate from the ORM entity in `app.models`. The default
`OpenFDAAdapter` persists results through `SQLAlchemyNoticeStore`; tests can
inject an HTTP client and a notice store.

The adapter searches `product_description` as a quoted phrase and pages through
results. `OPENFDA_TIMEOUT_SECONDS` defaults to 15 per HTTP operation;
`OPENFDA_MAX_RETRIES=3` allows four attempts per page. Timeouts, transport errors,
HTTP 408/429, and 5xx responses use exponential backoff. `Retry-After` is respected;
delays over 60 seconds fail with a message to try later. Other HTTP errors,
malformed responses, and missing recall identifiers are errors, not empty searches.

Each successfully mapped page commits an upsert keyed by `(source, recall_number)`
(`recall_number` maps to the table's `notice_id`). Refetches preserve the internal
UUID and refresh `fetched_at`, evidence, and status. Older concurrent observations
cannot overwrite newer ones. This stores the latest snapshot, not fetch history.
Earlier pages remain saved if a later page fails; the overall call raises instead
of returning incomplete results. Queries exceeding the skip limit also fail with
instructions to narrow the query.

Notice links are official record-specific API URLs without credentials. Missing
or invalid `report_date` values become `None` with the original value retained in
`raw_json`, so later matching/verification can treat the date as unknown.

Source behavior follows openFDA's [query syntax](https://open.fda.gov/apis/query-syntax/)
and [paging limits](https://open.fda.gov/apis/query-parameters/).

## Deterministic matching

`app.services.matcher.match(medicine, notice, brand_threshold=90.0)` returns a
Pydantic `Decision(verdict, reason, evidence)`. It accepts the Pydantic models in
`app.schemas` or the existing SQLAlchemy Medicine/Notice instances. It performs
no network calls, writes, or alert delivery. Install updated requirements first:

```powershell
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pytest tests/test_matcher.py -q
```

Names are lowercased, Unicode-normalized, and punctuation becomes whitespace.
The matcher scores complete word windows in `product_description` using
[RapidFuzz ratio](https://rapidfuzz.github.io/RapidFuzz/Usage/fuzz.html#ratio).
The default threshold is 90/100; set `brand_threshold=100` to require identical
normalized brand text. The score, selected candidate, and threshold are recorded.

Strengths normalize spacing, case, and decimal formatting: `500 mg`, `500MG`, and
`500.0mg` compare equally. Common units (`mg`, `g`, `mcg`/`µg`, `ml`, `l`, `IU`,
`units`, `%`) and explicit concentrations such as `250mg/5ml` are supported.
There is no mass/volume unit conversion: `0.5g` and `500mg` remain different.
A concentration never matches only its numerator. Multiple distinct strengths,
missing strengths, and unsupported formats return `UNCERTAIN`.

Batches compare as complete, case-sensitive identifiers, preserving punctuation
and leading zeros. `AB12` does not match `AB123`, `AB-12`, or `ab12`. Explicit lot
lists and bare lists of codes containing digits are supported; labeled lists also
accept uppercase alphabetic IDs. Expiration, NDC, and UPC metadata is excluded.
Unparseable prose, wildcard/all-lot statements, and ambiguous ranges return
`UNCERTAIN` rather than implying the batch is absent.

- `MATCH`: brand passes the threshold, normalized strength matches, and the exact
  batch is present.
- `NO_MATCH`: parseable evidence shows a different brand, strength, or batch.
  A matching brand with a different batch is always `NO_MATCH` when the notice
  evidence is parseable.
- `UNCERTAIN`: the household batch is missing, or required input/notice evidence
  is unparseable. Missing batch takes precedence and adds
  `evidence.requires_user_input = true`, `missing_fields`, and a `question` for the
  calling workflow to pause and ask. The pure matcher does not prompt directly.

Evidence includes source text, notice URL/date, normalized identifiers, comparison
results, and parsed batch tokens. A matcher `MATCH` still requires the separate
notice-verification and human-approval steps before any alert. Notice date
availability is checked by that later verification step, not by identity matching.

## Groq field extraction

`extract_notice_fields(text) -> ExtractedNotice | None` in
`app.services.extraction` requests JSON using the Groq SDK and validates it with
Pydantic. The accepted schema is `brand`, `strength`, `batch_numbers`, and
`confidence` (0–1). It has no verdict field, and extra fields are rejected. Use
`fields.model_dump(mode="json")` for a JSON-compatible dictionary or
`fields.model_dump_json()` for a JSON string.

Configure `GROQ_API_KEY` and `GROQ_MODEL` in `.env`; choose an available model that
supports [Groq JSON object mode](https://console.groq.com/docs/structured-outputs).
`GROQ_MIN_CONFIDENCE` defaults to 0.90, `GROQ_TIMEOUT_SECONDS` to 15, and
`GROQ_MAX_RETRIES` to 2. No additional dependency is needed beyond `requirements.txt`.

The extractor returns `None` for missing configuration, HTTP/timeout failures,
invalid JSON/schema, incomplete or refused completions, missing fields, and low
confidence. Fields must occur in the input text; batch identifiers must appear as
complete, case-sensitive tokens. Duplicate IDs are rejected. Inputs over 32,000
characters are rejected rather than truncated. Confidence is self-reported and
is only an acceptance gate, not proof of correctness.

Pass the result to `match_extracted(medicine, notice, fields)`. Explicitly passing
`None` yields `UNCERTAIN`, even if the original notice could otherwise match.
Accepted fields are rechecked against the original notice and sent through the
same deterministic matcher. The wrapper rejects fields from the wrong source
section, omitted parseable batch lists, conflicting strengths, and shortened
concentrations. Original source text and extraction evidence are kept in the
decision. The LLM never receives household identity or the target batch and never
produces the final verdict.

Example fallback for existing `medicine` and `notice` objects:

```python
from app.core.config import GroqSettings
from app.models.enums import Verdict
from app.services.extraction import extract_notice_fields
from app.services.matcher import match, match_extracted

decision = match(medicine, notice)
if decision.verdict == Verdict.UNCERTAIN and not decision.evidence["requires_user_input"]:
    settings = GroqSettings()
    text = (notice.product_description or "") + "\n" + (notice.code_info or "")
    fields = extract_notice_fields(text, settings=settings)
    decision = match_extracted(
        medicine, notice, fields, min_confidence=settings.groq_min_confidence
    )
# If requires_user_input is true, the caller pauses and asks evidence["question"].
# Human approval and independent notice verification still precede any alert.
```

The default `match()` never makes an LLM call. Tests in `tests/test_extraction.py`
use three synthetic notice texts and mocked Groq HTTP responses, with no API key
or network required. They test the contract and failure paths, not live model accuracy.

## Schema choices

- All primary keys are UUIDs. `household_id` is a UUID supplied by the application;
  there is no households table in this requested schema.
- `decisions.notice_id` references the internal `notices.id` UUID. The external
  `notices.notice_id` is a string, unique together with `source` (for openFDA, use
  `recall_number`). Keep a record-specific official API URL in `notices.url`.
- `medicines.name` is the brand. Store strength and batch as text, preserving batch
  leading zeros. A NULL batch means user input is required, never a wildcard.
- `notices.status` preserves the regulator's status string. Missing source fields
  are nullable. Parse openFDA `report_date` (`YYYYMMDD`) into a SQL `DATE`; never
  substitute the fetch date for the notice date.
- Timestamps are timezone-aware and default to the database transaction time.
  Source payloads, decision evidence, and evaluation records use PostgreSQL JSONB.
  Assign a new JSON object when updating it so SQLAlchemy tracks the change.
- Verdicts are `MATCH`, `NO_MATCH`, or `UNCERTAIN`. Action states are
  `PENDING_APPROVAL`, `APPROVED`, `REJECTED`, `SENT`, and `FAILED`. Verification
  results are `CONFIRMED`, `NOT_CONFIRMED`, or `UNCERTAIN`.
- Actions begin pending approval and have a unique idempotency key. Approved/sent
  rows require a nonblank approved message. A confirmed verification requires a
  URL, date, and `batch_found = true`. Foreign keys restrict deletion of referenced
  audit records. Multiple decisions and verification attempts are retained.
- `test_runs.actual`, `evidence`, and `passed` can be NULL until evaluated. Use
  JSON objects for input/output fields, e.g. `{"verdict": "NO_MATCH"}`.

## Implemented workflow

The graph executes `collect -> plan -> retrieve -> match -> approve -> act -> verify`:

1. Pause and ask for a missing batch number before checking recalls.
2. Require exact brand + strength + batch evidence for `MATCH`. A different batch
   is `NO_MATCH`; missing evidence is `UNCERTAIN`.
3. Propose an alert only for a confirmed match, including the official notice URL
   and notice date. Require explicit human approval of the message before sending.
4. Commit the approved mock delivery once, using the unique key
   `medicine_id:notice_id:source_status`. The action row is the mock receipt.
5. Independently reopen the official notice after the mock send, as requested by
   the node order. Recheck brand, strength, batch, date, and status; save each
   verification attempt. Failures remain visible in checkpoints, API responses,
   database records, and logs. CAPTCHA/OTP/redirect pages are not bypassed.

The API adds a `runs` table to persist household membership, queue status, replies,
and the latest state of each medicine. LangGraph maintains its own checkpoint
tables. Rerun `scripts.init_db` to create the new application table; API/CLI startup
initializes the checkpoint tables. API requests use async SQLAlchemy sessions;
synchronous graph operations execute in worker threads with separate sessions.

This local hackathon API has no authentication/household authorization layer;
use synthetic data on localhost. Approval requests are explicit human decisions.
Alerts are mock DB receipts and log entries. A crash after the DB receipt commits
can omit its console log; it cannot create a duplicate receipt. Real external
delivery would require an outbox/provider idempotency integration.

Only synthetic household data is included, and initialization does not seed data
or send alerts. AWS EC2 deployment is outside this scaffold.

## Validation

```powershell
.venv\Scripts\python -m pytest
# Optional real PostgreSQL checks (isolated temporary schema, removed afterward):
$env:TEST_DATABASE_URL = 'postgresql+psycopg://drug_monitor:local_dev_change_me@localhost:5432/drug_monitor'
.venv\Scripts\python -m pytest -m integration
```

Integration checks create a randomly named schema and remove only that schema.
Use a dedicated test database for CI. The included GitHub Actions workflow does so.

Reference: [SQLAlchemy typed declarative models](https://docs.sqlalchemy.org/en/20/orm/declarative_tables.html)
and [PostgreSQL types](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html).
Data source: [openFDA drug enforcement API](https://open.fda.gov/apis/drug/enforcement/).

Daily monitoring starts with the FastAPI worker (09:00 Asia/Kolkata by default).
See [scheduler configuration, change logs and approval behavior](docs/scheduler.md).

# Drug Recall Monitor

A hackathon app that checks a synthetic household's medicines against public
openFDA drug recall notices. Users enter the brand, strength and batch number;
the app saves the evidence, pauses for human review and records approved mock alerts.

**Groq extracts notice fields when needed. Python rules decide the verdict.
Human approval is required before mock delivery.**

The project uses Python 3.11+, FastAPI, LangGraph, Groq, PostgreSQL, SQLAlchemy,
Streamlit, APScheduler, Docker Compose, GitHub Actions and optional LangSmith tracing.

## What is implemented

- Add and edit household medicines, preserving exact batch identifiers.
- Retrieve and save openFDA notices with retries, timeouts and pagination.
- Return `MATCH`, `NO_MATCH` or `UNCERTAIN` with source evidence.
- Ask for a missing batch before fetching notices.
- Optionally use Groq to extract facts from uncertain notice text.
- Require separate human confirmation of decisions and approval of alerts.
- Resume interrupted runs using PostgreSQL checkpoints and a durable run queue.
- Record mock delivery once and independently reopen the official notice afterward.
- Recheck medicines daily while the API is running; detect changed notices separately
  from failed fetches and request fresh approval for applicable updates.
- Run TC1–TC4 and save inputs, expectations, actual results, evidence and failures.

This is a synthetic-data demonstration. It has no household authentication or
access-control layer and sends no real email, SMS or push notifications.

## Matching and approval rules

| Verdict | Meaning |
| --- | --- |
| `MATCH` | Brand passes the configured similarity threshold, normalized strength matches, and the exact batch is present. |
| `NO_MATCH` | Parseable notice evidence shows a different brand, strength or batch. The same brand with a different batch does not match. |
| `UNCERTAIN` | Required evidence is absent, ambiguous or unparseable. This is not a statement that the medicine is unaffected. |

Names are normalized and compared with RapidFuzz; the default brand threshold is
90/100. Set the pure matcher's `brand_threshold=100` to require identical normalized
brand text. Batch matching is exact and case-sensitive: `AB12`, `AB123`, `AB-12`
and `ab12` are different identifiers. Leading zeros are preserved.

Strength spacing and decimal formatting normalize (`500 mg` equals `500mg`).
Units are not converted: `0.5g` and `500mg` remain different. Unsupported strengths,
multiple distinct strengths, ambiguous lot ranges and all-lot statements fail
closed as `UNCERTAIN`.

The graph runs in this order:

```text
collect -> plan -> retrieve -> match -> confirm -> approve -> act -> verify
```

| Step | Behavior |
| --- | --- |
| Collect | If the household batch is missing, interrupt and ask the user. |
| Plan | Record the source and match scope: brand + strength + batch. |
| Retrieve | Fetch openFDA candidates and persist the notices. |
| Match | Apply deterministic rules; optionally extract facts with Groq for uncertain notices. |
| Confirm | Ask the human to Accept or Reject each matcher decision. |
| Approve | For accepted MATCH decisions, propose a dated alert with the official link and ask for approval. |
| Act | Record the approved mock delivery with an idempotency key. |
| Verify | Reopen the official notice independently and record the batch, date and verification result. |

**Accepting an UNCERTAIN decision does not turn it into MATCH.** Decision review
and alert approval are different steps. Every displayed decision needs a choice
before the confirmation response is submitted; the same applies to multiple alerts.

Only a MATCH with an official notice URL and date may propose an alert. Approved
message edits must retain that URL and date. Verification happens **after** mock
sending in the requested node order; a verification failure remains visible and
does not retroactively erase the mock receipt. CAPTCHA, OTP and access barriers
are not bypassed.

## Repository structure

```text
app/
  main.py                 FastAPI application, worker and scheduler lifecycle
  agents/                 Graph nodes, PostgreSQL checkpoints and audit repository
  api/                    Async inventory, run and human-review endpoints
  core/config.py          Environment configuration
  db/                     SQLAlchemy engines, sessions and declarative base
  models/                 Inventory, notices, decisions, actions, verifications and runs
  schemas/                Pydantic input, response and extraction contracts
  services/               Matching, extraction, verification, queue, scheduler and tests
  sources/                Source interface and openFDA adapter
frontend/
  app.py                  Streamlit entry point
  views/                  Medicine list, Run monitor, Approvals, Results and Test cases
scripts/init_db.py        Repeatable application-table bootstrap
fetch.py                  Fetch and persist source candidates
run_agent.py              Start, inspect and resume a single-medicine graph
run_api.py                API launcher with Windows async-driver support
run_official_tests.py     Execute and save TC1–TC4
requirements.txt          Python runtime and test dependencies
.python-version          Python version for deployment
.env.example             Configuration template; use placeholder values only
docker-compose.yml       Local PostgreSQL service and persistent volume
data/synthetic/          Synthetic household data
data/official_tests/     Recorded FDA response, provenance and labelled status fixture
tests/                   Unit and isolated PostgreSQL integration tests
docs/                    Additional usage and configuration guides
.github/workflows/ci.yml Python 3.11 / PostgreSQL 16 CI checks
```

Docker Compose currently runs **PostgreSQL only**. Start the API and UI separately.

## Run locally on Windows

Prerequisites: Python 3.11+, Git and a running Docker Desktop installation.
Use PowerShell from the project folder.

### 1. Install dependencies

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

### 2. Create a local `.env`

Create `.env` in the repository root. The values below are local development
examples; replace the password for any shared environment. Do not overwrite an
existing `.env` containing your configuration.

```dotenv
POSTGRES_USER=drug_monitor
POSTGRES_PASSWORD=local_dev_change_me
POSTGRES_DB=drug_monitor
POSTGRES_PORT=5432
DATABASE_URL=postgresql+psycopg://drug_monitor:local_dev_change_me@127.0.0.1:5432/drug_monitor
DRUG_MONITOR_API_URL=http://127.0.0.1:8000
GROQ_EXTRACTION_ENABLED=false
LANGSMITH_TRACING=false
```

The database URL must match the PostgreSQL credentials. URL-encode special
characters in its password. The explicit IPv4 address avoids localhost/IPv6
connection issues on some Windows installations.

### 3. Start the database and API

```powershell
docker compose up -d --wait postgres
.venv\Scripts\python -m scripts.init_db
.venv\Scripts\python run_api.py
```

The initializer creates missing application tables and enums without deleting
records. API startup initializes LangGraph checkpoint tables. This is a bootstrap,
not a migration system for changing existing columns.

Check [API health](http://127.0.0.1:8000/health) for `{"status":"ok"}` and open
[interactive API docs](http://127.0.0.1:8000/docs). Health is a liveness check,
not a guarantee that every database or source request will succeed.

### 4. Start Streamlit in a second terminal

```powershell
.venv\Scripts\python -m streamlit run frontend/app.py
```

Open [the local UI](http://127.0.0.1:8501). Keep both terminals running.
No Groq key is required for parser-only matching.

## Use the app

1. **Medicine list:** Add the brand, strength and batch exactly as printed. A blank
   batch is allowed in inventory and causes a question when a check starts.
2. **Run monitor:** Start a check and view saved progress. The UI polls real state
   every two seconds. Source search hits are candidates, not confirmed recalls.
3. **Approvals:** Supply missing batches, review every decision, then approve or
   reject any proposed MATCH alert. The backend preserves pending questions across restarts.
4. **Results:** Inspect sent mock alerts, notice links, non-matches, uncertain items
   and verification failures. A pending or rejected alert is not shown as sent.
5. **Test cases:** Run TC1–TC4 and inspect their saved inputs, expected and actual
   behavior, evidence and pass/fail explanations.

The sidebar's default synthetic household is
`00000000-0000-4000-8000-000000000001`. Initialization does not seed inventory or
send alerts. Editing inventory is blocked while a check involving it is active.
Start a new check after changing matching configuration; existing checkpointed
decisions retain their original evidence.

## Optional Groq extraction

Set these on the **backend** in `.env` or Render's Environment settings:

```dotenv
GROQ_EXTRACTION_ENABLED=true
GROQ_API_KEY=YOUR_PRIVATE_GROQ_KEY
GROQ_MODEL=YOUR_AVAILABLE_JSON_MODE_MODEL_ID
GROQ_MIN_CONFIDENCE=0.9
```

Create a key in the [Groq console](https://console.groq.com/keys) and select a
JSON-mode model available to your account from the
[current model list](https://console.groq.com/docs/models). Keep actual keys out
of GitHub, `.env.example`, Streamlit secrets and shared logs.

The live graph calls `match_notice()` in `app/services/notice_matching.py`:

- Clear parser MATCH/NO_MATCH results do not call Groq.
- Uncertain notice evidence may call `extract_notice_fields()` when extraction is enabled.
- Groq receives only the public product description and `code_info`, not household
  identity or the target medicine's batch.
- Pydantic, confidence and literal-source checks validate the extracted brand,
  strength and batch list. Python's `match_extracted()` computes the verdict.
- Missing configuration, failed calls, low confidence, invented fields and unresolved
  ambiguity leave the decision UNCERTAIN. A key cannot supply missing source evidence.

The confidence threshold defaults to 0.9, timeout to 15 seconds and retries to 2.
Self-reported confidence is an acceptance gate, not proof of correctness. Inputs
over 32,000 characters are rejected rather than truncated.

Check `GROQ_EXTRACTION_STARTED`, `GROQ_EXTRACTION_COMPLETED` or
`GROQ_EXTRACTION_SKIPPED` in backend logs. Decision evidence includes extraction
status; review cards explain validated, unusable or missing-configuration results.
See [Groq deployment details](docs/groq-deployment.md).

## Daily monitoring and notice updates

APScheduler queues all medicines daily at **09:00 Asia/Kolkata** by default.
Each medicine gets a separate durable run, so one unanswered batch question cannot
block the others. Run IDs deduplicate the same medicine and local calendar day.
Startup after the scheduled time queues today's missing checks, not earlier days.

```dotenv
SCHEDULER_ENABLED=true
SCHEDULER_HOUR=9
SCHEDULER_MINUTE=0
SCHEDULER_TIMEZONE=Asia/Kolkata
SCHEDULER_DESTINATION=mock://synthetic-household
```

| Log event | Meaning |
| --- | --- |
| `NOTICE_CHANGED` | A committed notice's status or batch evidence changed. |
| `FETCH_FAILED` | A source request failed; network failures have `category=network_error`. |
| `DAILY_CHECKS_QUEUED` | The scheduler committed new run records. |
| `DAILY_QUEUE_FAILED` | Enqueuing failed, separately from source fetching. |

First observations establish a baseline. Reordered batch lists are equivalent;
stale observations cannot overwrite newer ones. A failed fetch is never reported
as NO_MATCH. Successfully saved pages remain saved if a later page fails, but that
incomplete retrieval stops before matching and proposing alerts.

For a current exact MATCH, changed status or batch evidence produces a new pending
update through the same confirmation and approval flow. The action key extends
`medicine_id:notice_id:status` with a revision fingerprint. Repeated checks reuse
the current action; changed versions, including returns to earlier versions,
require fresh approval. Old audit records remain intact, and stale notice snapshots
cannot be approved or sent. A removed batch producing NO_MATCH does not send an alert.

**The API process must be running for the timer to fire.** Sleeping free hosting
does not provide reliable daily execution. The GitHub Actions workflow currently
runs CI tests; a cloud scheduling replacement has not been implemented.
See [scheduler configuration and semantics](docs/scheduler.md).

## Deploy: Neon + Render + Streamlit Community Cloud

This setup separates persistent data, the API/agent and the browser UI. Choose
free plans where available and review each provider's current quotas.

### 1. Prepare GitHub

Push the repository, including `.python-version`, `requirements.txt`, `app/`,
`frontend/`, `scripts/` and `data/`. Do not upload `.env`, secret files or real keys.
The version file pins Python 3.11; no local `secrets.toml` is required for Cloud setup.

### 2. Create Neon PostgreSQL

Create a project in the [Neon console](https://console.neon.tech/). Click **Connect**
and turn **Connection pooling off** to obtain a direct connection. This project
uses session advisory locks and a PostgreSQL checkpointer.

For SQLAlchemy, change only the URL prefix from `postgresql://` to
`postgresql+psycopg://`. Preserve the credentials, hostname, database and SSL query
parameters. Save that value privately as `DATABASE_URL`; never publish it.
[Neon connection reference](https://neon.com/docs/connect/connect-from-any-app).

### 3. Deploy the API on Render

Create a **Web Service** from this GitHub repository:

| Setting | Value |
| --- | --- |
| Runtime | Python 3 |
| Branch | `main` |
| Root directory | Leave blank |
| Build command | `pip install -r requirements.txt` |
| Start command | `python -m scripts.init_db && python run_api.py --host 0.0.0.0 --port $PORT` |
| Health check path | `/health` |

Add `DATABASE_URL` in **Environment**. Set `LANGSMITH_TRACING=false` unless tracing
is configured. Add the Groq settings above if extraction is desired. Deploy and
wait for **Live**, then test `https://YOUR-SERVICE.onrender.com/health`.
The root path may return Not Found; use `/docs` for the API interface.

Project backend: [health](https://drug-recall-api.onrender.com/health) and
[API documentation](https://drug-recall-api.onrender.com/docs). Availability depends
on the hosting service and configuration; these are API links, not the Streamlit UI.

### 4. Deploy the UI on Streamlit Community Cloud

At [share.streamlit.io](https://share.streamlit.io/), create an app with:

| Setting | Value |
| --- | --- |
| Repository | `Sahil123goyal/Drug_monitor_recall` |
| Branch | `main` |
| Main file | `frontend/app.py` |
| Python version in Advanced settings | `3.11` |

In **Advanced settings > Secrets**, enter the backend base URL:

```toml
DRUG_MONITOR_API_URL = "https://drug-recall-api.onrender.com"
```

Use your own backend URL when deploying a fork. Do not append `/health` or `/docs`.
Root-level Streamlit secrets become environment variables, which the existing
API client reads. PostgreSQL and Groq credentials belong on Render, not in the UI.
Click **Deploy**, then test adding a synthetic medicine and starting a check.
[Streamlit deployment reference](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy).

**Free-hosting limit:** Render Free sleeps after inactivity, so APScheduler may not
run on time; Streamlit Community Cloud can also hibernate. The database retains
runs and approvals, but free quotas and cold starts still apply. Open the backend's
`/health` and wait for it to respond before retrying a UI request after a cold start.
[Render limits](https://render.com/docs/free).

## API and command-line tools

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/health` | API liveness |
| GET / POST | `/medicines` | List household inventory / add a medicine |
| PUT | `/medicines/{medicine_id}` | Edit a medicine when no active check blocks it |
| GET / POST | `/runs` | List household checks / queue a check |
| GET | `/runs/{run_id}` | Read state, pending question, decisions and actions |
| POST | `/runs/{run_id}/provide-batch` | Answer a missing-batch question |
| POST | `/runs/{run_id}/confirm-decisions` | Accept or reject every displayed decision |
| POST | `/runs/{run_id}/approve` | Approve or reject every proposed alert |
| GET | `/test-runs` | Read saved evaluation reports |
| POST | `/test-runs/evaluate` | Execute and save an evaluation suite |

Human replies include the current interrupt ID. Stale replies are rejected;
accepted replies are committed before the queue worker resumes the graph.
See [API examples](docs/api.md) and interactive `/docs` for request schemas.

```powershell
# Fetch and persist candidates, not final match verdicts:
.venv\Scripts\python fetch.py "ibuprofen"

# A missing batch pauses this synthetic single-medicine CLI run:
.venv\Scripts\python run_agent.py start --thread-id synthetic-demo-1 --name DemoRelief --strength "500 mg"
.venv\Scripts\python run_agent.py status --thread-id synthetic-demo-1

# Save a human response matching the displayed question in reply.json:
.venv\Scripts\python run_agent.py resume --thread-id synthetic-demo-1 --reply-file reply.json
```

The adapter searches openFDA `product_description`, maps `recall_number` to the
external notice ID, saves raw JSON and uses credential-free record URLs. It retries
transport errors and HTTP 408/429/5xx, handles successful empty searches separately,
and raises on incomplete pagination. Only the latest notice snapshot is stored;
earlier decision snapshots preserve assessed evidence.

Source: [openFDA drug enforcement API](https://open.fda.gov/apis/drug/enforcement/).

## Official acceptance cases

The repository includes a recorded real FDA response for recall `D-0047-2025`:
IBU 600 mg, lot `C5406201`, report date `2024-11-27`. Provenance and its SHA-256
checksum are in `data/official_tests/manifest.json`. This is historical source data,
not a guarantee of the notice's current status.

| Case | Input / scenario | Expected behavior |
| --- | --- | --- |
| TC1 | Real product, strength and recalled lot | Dated alert proposed; human alert approval still required |
| TC2 | Same brand and strength, different synthetic batch | No alert |
| TC3 | Clearly labelled fixture changes Ongoing to Terminated | Updated alert proposed; fresh approval required |
| TC4 | Household batch missing | Ask the user before retrieval |

TC3 is a **simulated status transition**, not an observed FDA withdrawal.
Terminated denotes recall closure under FDA criteria; it does not mean withdrawal
or establish that a medicine is safe. See the
[FDA status definitions](https://www.fda.gov/safety/enforcement-reports/enforcement-report-information-and-definitions).

```powershell
# Reproducible replay of the recorded FDA response:
.venv\Scripts\python run_official_tests.py --source snapshot

# Fetch the selected recall live for TC1/TC2; TC3 remains a labelled fixture:
.venv\Scripts\python run_official_tests.py --source live --report official-results.json
```

Each attempt appends four reports to `test_runs`, including input, expected, actual,
evidence, pass/fail and an explanation for mismatches. The suite simulates decision
confirmation only inside its rollback sandbox, stops at alert approval and never
approves or sends an alert. Temporary inventory, decisions and actions roll back;
the reports are committed separately. Exit codes are 0 for all passing, 1 for case
failures and 2 for a suite/storage failure. CLI defaults to live; the UI and API
use snapshot mode by default. Legacy matcher smoke cases remain available through
`POST /test-runs/evaluate?suite=core`.

## Database, tracing and tests

Application tables: `medicines`, `notices`, `decisions`, `actions`, `verifications`,
`test_runs` and `runs`. LangGraph creates separate checkpoint tables. Primary keys
are UUIDs; `decisions.notice_id` references the internal notice UUID, distinct from
the external FDA recall number. JSONB stores source payloads and audit evidence.
Actions start pending approval and their idempotency keys are unique.

To enable LangSmith, configure these on the backend:

```dotenv
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=YOUR_PRIVATE_LANGSMITH_KEY
LANGSMITH_PROJECT=drug-recall-monitor
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
```

Tracing is optional and disabled unless configured. It can include workflow inputs
and evidence; use synthetic household data.

```powershell
# Unit tests; database integration checks skip if TEST_DATABASE_URL is absent:
.venv\Scripts\python -m pytest

# Include integration checks against a disposable PostgreSQL database:
$env:TEST_DATABASE_URL = 'postgresql+psycopg://drug_monitor:local_dev_change_me@127.0.0.1:5432/drug_monitor'
.venv\Scripts\python -m pytest -m integration
```

Integration tests create and remove randomly named test schemas. Use a dedicated
test database. The GitHub Actions workflow runs the suite with PostgreSQL 16 and
checks that initialization is repeatable. Groq unit tests use mocked HTTP responses;
they validate integration and failure handling, not live model accuracy.

## Current boundaries

- No authentication or household authorization: public access is for synthetic
  demonstrations, not real household medical records or trusted production approvals.
- Alerts are mock database receipts and logs. Real delivery needs a provider and
  transactional outbox/provider idempotency integration.
- Verification occurs after mock delivery and may fail; failures remain visible.
- Conservative parsing and extraction can leave notices UNCERTAIN; they cannot
  invent missing strengths or lots or turn human acceptance into a different verdict.
- The daily timer needs a running API; free sleeping hosts do not guarantee its schedule.
- No automatic migration system, production backup policy or full application
  Docker/EC2 deployment is included. Docker Compose provisions local PostgreSQL.
- Cloud PostgreSQL starts with its own data; local inventory is not automatically copied.

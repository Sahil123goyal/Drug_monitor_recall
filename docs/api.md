# FastAPI usage

From the project directory, with `.env` configured:

```powershell
docker compose up -d --wait postgres
.venv\Scripts\python -m scripts.init_db
.venv\Scripts\python run_api.py
```

Browse `http://127.0.0.1:8000/docs` for the complete Pydantic/OpenAPI contracts.
The launcher supports `--host` and `--port`. It selects the event loop required
by Psycopg async connections on Windows. On Linux, Uvicorn can also be launched
directly: `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`.

| Endpoint | Success | Purpose |
| --- | --- | --- |
| `POST /medicines` | 201 | Add an inventory item; `batch_no` can be null/omitted. |
| `GET /medicines?household_id=UUID` | 200 | List the household inventory. |
| `PUT /medicines/{id}` | 200 | Replace brand (`name`), strength, and batch; active checks block edits. |
| `POST /runs` | 202 | Queue all current medicines in the specified household. |
| `GET /runs?household_id=UUID` | 200 | List recent household checks (optional limit 1–100). |
| `GET /runs/{id}` | 200 | Read status, per-medicine state, pending interrupt, decisions. |
| `POST /runs/{id}/approve` | 202 | Persist approval/rejection for each displayed alert and resume. |
| `POST /runs/{id}/provide-batch` | 202 | Persist the answer to the current missing-batch question. |
| `GET /test-runs?limit=50&offset=0` | 200 | Read saved evaluations; limit 1–100, offset >= 0. |
| `GET /test-runs?suite=core` | 200 | Read the latest saved TC1–TC4 evaluations. |
| `POST /test-runs/evaluate` | 200 | Execute and upsert TC1–TC4 with synthetic notices. |

Unknown IDs/empty households return 404. Invalid bodies return 422. Stale replies,
wrong interrupt types, and replies while a run is not WAITING return 409.
Unknown request fields are rejected. `approved` must be a JSON boolean.

## Synthetic household example

In another PowerShell terminal:

```powershell
$base = 'http://127.0.0.1:8000'
$household = '00000000-0000-4000-8000-000000000001'
$medicine = Invoke-RestMethod "$base/medicines" -Method Post -ContentType 'application/json' -Body (@{
    household_id = $household
    name = 'DemoRelief'
    strength = '500 mg'
    batch_no = $null
} | ConvertTo-Json)
$run = Invoke-RestMethod "$base/runs" -Method Post -ContentType 'application/json' -Body (@{
    household_id = $household
    destination = 'mock://synthetic-household'
} | ConvertTo-Json)
$run.id
```

The synthetic name may have no live openFDA results. Matching/approval examples
use synthetic notices in the automated tests; never fabricate a public recall.

Poll `GET /runs/{id}` until `status` is `WAITING`, `COMPLETED`, or `FAILED`:

```powershell
$current = Invoke-RestMethod "$base/runs/$($run.id)"
$current | ConvertTo-Json -Depth 20
```

If `pending_interrupt.value.kind` is `collect`, answer that exact interrupt:

```powershell
Invoke-RestMethod "$base/runs/$($run.id)/provide-batch" -Method Post -ContentType 'application/json' -Body (@{
    interrupt_id = $current.pending_interrupt.id
    batch_no = 'SYN001'
} | ConvertTo-Json)
```

If `pending_interrupt.value.kind` is `approve`, review each item in
`pending_interrupt.value.alerts`. Each includes `action_id`, `destination`,
`message`, and `idempotency_key`. Send every displayed action exactly once:

```json
{
  "interrupt_id": "COPY_CURRENT_INTERRUPT_ID",
  "approvals": [
    {
      "action_id": "COPY_DISPLAYED_ACTION_UUID",
      "approved": false
    }
  ]
}
```

Use `true` only after human approval. Optional `destination` and `message` fields
replace the proposed version. The message must retain the complete official URL
and ISO notice date. Rejections create no delivery or verification. The graph
stores accepted edits in `actions.approved_message` before mock delivery.

## Durable execution and response semantics

Each run freezes the household's medicine IDs, then processes them sequentially.
An unanswered question pauses that run. Each medicine uses the stable LangGraph
thread ID `<run_id>:<medicine_id>`; the CLI's standalone threads are separate.

`status` is QUEUED, RUNNING, WAITING, COMPLETED, or FAILED. `state` maps medicine
UUIDs to their last saved graph state and includes source plan, decisions, failures,
and verification results, updated after each graph node. `actions` includes the
actual approval/delivery status, proposed/approved versions, and notice URL/date;
this lets the UI distinguish a proposed MATCH from a sent alert. `decisions` contains only this run's decisions, with
evidence. A completed run can still contain UNCERTAIN decisions with a
`NEEDS_REVIEW` medicine outcome; completion does not imply absence of a recall.
Source errors and verification failures are never reported as a successful
no-match result. `GET` returns the last persisted snapshot while work is running.

The API commits replies before returning 202. A polling worker picks up jobs
within approximately one second. On restart it recovers QUEUED/RUNNING jobs;
WAITING jobs require human input. Row locks serialize competing replies, and
PostgreSQL advisory locks prevent concurrent workers from advancing the same run.
A saved reply is applied only to its original checkpoint interrupt ID. FAILED
runs remain inspectable; start a new assessment after resolving the failure.

No request holds an open transaction while waiting for a human or performing
source HTTP requests. A separate async session is used for each request and each
worker transaction. The API currently assumes a trusted local caller; household
authorization is a separate deployment concern.

Implementation: [routes](../app/api/routes.py), [schemas](../app/schemas/api.py),
[async sessions](../app/db/async_session.py), [worker](../app/services/run_worker.py).
The session lifecycle follows the
[SQLAlchemy async session documentation](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html).

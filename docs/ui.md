# Streamlit UI

Requires Python 3.11+, the API, and PostgreSQL. From the project directory:

```powershell
# First setup only: keep credentials in .env, not a shared template.
Copy-Item .env.example .env
.venv\Scripts\python -m pip install -r requirements.txt
docker compose up -d --wait postgres
.venv\Scripts\python -m scripts.init_db
.venv\Scripts\python run_api.py
```

In a second terminal:

```powershell
.venv\Scripts\python -m streamlit run frontend/app.py --server.address 127.0.0.1
```

Open http://127.0.0.1:8501. Restart the API after updating this project to load the
new inventory/history/action endpoints. The UI reads `DRUG_MONITOR_API_URL` from
`.env`, defaulting to `http://127.0.0.1:8000`. It never connects directly to Postgres.

The sidebar selects a household UUID; the default is the synthetic household
`00000000-0000-4000-8000-000000000001`. This remains a trusted localhost demo with
mock deliveries. No provider credentials are entered into the UI.

## Pages

1. **Medicine list:** Add a brand, strength, and optional batch, or edit an existing
   medicine. Blank batches are stored as missing, not as wildcard matches. Editing
   is blocked while a check involving that medicine is queued, running, or waiting;
   use Approvals to answer a requested batch. Historical results retain the
   medicine snapshot assessed by that check. If a changed identity conflicts with
   an alert already recorded for the same medicine/notice/status, add a new inventory
   item; the old batch's delivery is never reused as delivery for the new batch.
2. **Run monitor:** Start a check and select a recent household run. Every two
   seconds the UI displays the real saved completion of collect, plan, retrieve,
   match, approve, act, and verify. Waiting/failed steps stay visible. Polling does
   not start another check or fabricate progress.
3. **Approvals:** A missing-batch interrupt displays a batch input. An approval
   interrupt shows the exact proposed message, destination, official notice link,
   and date. Approve and Reject buttons have no default selection. For multiple
   alerts, every item must be reviewed; choices are submitted together after the
   last explicit click. Missing/invalid links or dates disable approval. Stale
   interrupt responses are rejected by the API.
4. **Results:** Display actual sent mock alerts with their notice links, completed
   no-match searches, and every UNCERTAIN item. A MATCH awaiting approval is not
   displayed as sent, and a rejected alert is not displayed as no match.
   Incomplete checks and verification failures have distinct visible states.
5. **Test cases:** Inspect TC1–TC4 input, expected result, actual result, evidence,
   and pass/fail. Before execution the results say **Not run**. Click **Run TC1–TC4**
   to evaluate the real deterministic matcher and save the results in `test_runs`.

| Case | Synthetic scenario | Expected |
| --- | --- | --- |
| TC1 | Brand + strength + batch match | MATCH |
| TC2 | Same brand/strength, another batch | NO_MATCH |
| TC3 | Household batch missing | UNCERTAIN; user input required |
| TC4 | Notice batch text unparseable | UNCERTAIN |

The suite makes no regulator requests, invokes no LLM, and sends no alerts.
Repeated execution updates these four stable case IDs. These are matcher
evaluations; separate pytest tests cover graph interrupts, approval, and delivery.

## Validation

```powershell
.venv\Scripts\python -m pytest
```

Streamlit AppTest exercises add/edit, start, missing batch, Approve/Reject,
read-only refreshes, results, and test-case execution. PostgreSQL integration
checks additionally require `TEST_DATABASE_URL` and a running test database.

Implementation: [UI entry](../frontend/app.py), [pages](../frontend/views/),
[HTTP client](../frontend/api_client.py), [presentation rules](../frontend/presentation.py).
The live page refreshes use [Streamlit fragments](https://docs.streamlit.io/develop/concepts/architecture/fragments);
widget tests use [Streamlit AppTest](https://docs.streamlit.io/develop/api-reference/app-testing/st.testing.v1.apptest).

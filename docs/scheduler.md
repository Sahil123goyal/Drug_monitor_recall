# Daily medicine checks

Install `requirements.txt`, initialize the existing database, and run
`python run_api.py`. FastAPI starts APScheduler with the queue worker and stops
it on shutdown. The API process must be running for the timer to fire.
No database migration is required.

Configuration in `.env` (defaults shown):

```dotenv
SCHEDULER_ENABLED=true
SCHEDULER_HOUR=9
SCHEDULER_MINUTE=0
SCHEDULER_TIMEZONE=Asia/Kolkata
SCHEDULER_DESTINATION=mock://synthetic-household
```

Each day at 09:00 local time, every saved medicine gets its own durable `Run`.
One medicine waiting for its batch number cannot block another medicine's check.
Run IDs are derived from the local calendar date and medicine ID. PostgreSQL
deduplicates repeated triggers and competing API workers. Startup after the
scheduled time queues today's checks if missing; earlier days are not replayed.
New medicines added after the trigger are picked up the next day or by a manual
check. Queue failures roll back the enqueue transaction and remain logged;
restart the API after fixing the database to retry today's enqueue.

Find scheduled runs in the existing household run list, API `GET /runs`, and
Streamlit Approvals page. Runs use the normal graph, including missing-batch
interrupts, decision confirmation, alert approval and independent verification.
The scheduler never provides a human reply. The destination is shown for review
and can be edited during approval. Delivery remains mock-only.

## Changes and failures

- `NOTICE_CHANGED`: emitted after committing a change to status or batch
  evidence, with before/after values and the source notice ID. Reordered or
  duplicated batch tokens are equivalent. Batch case, punctuation and leading
  zeros are preserved. A first fetch establishes the baseline. Older fetches
  cannot overwrite newer observations. Unparseable batch text is retained and
  compared as text, with `batches=null`; it is never treated as an empty lot list.
- `FETCH_FAILED`: emitted once after exhausted retries or an unusable source
  response. Timeouts/transport failures carry `category=network_error`;
  exhausted HTTP failures carry `http_error`. The worker records a failed run,
  never `NO_MATCH`. An empty successful search is neither a change nor failure.
- `DAILY_QUEUE_FAILED`: scheduler/database enqueue failure, separate from
  source failures. `DAILY_CHECKS_QUEUED` records the number of newly queued runs.

The adapter commits complete fetched pages. A later pagination failure can
therefore produce both a committed change log and a fetch failure. That run
stops before matching or proposing alerts; the next successful check evaluates
the notices. Logs exclude API keys and authenticated request URLs.

## Updated alerts

Every successful check evaluates notices even if another household already
fetched the change. For a current exact MATCH, a new status or batch list creates
a separate pending action with an updated message, official link and notice
date. Existing actions and approvals remain in the audit trail. Batch removal
that makes the medicine NO_MATCH does not send a recall alert; UNCERTAIN evidence
also cannot authorize an alert.

Action keys extend `medicine_id:notice_id:status` with a content fingerprint so
batch-only updates cannot accidentally reuse a sent receipt. Repeated checks of
the current notice version reuse the pending/sent/rejected action. The fingerprint
includes the preceding proposal ID when a version changes, so a return to an
earlier version also requires a new approval. Existing legacy
keys are recognized from their decision snapshots. A stale snapshot cannot be
approved or sent after the stored status/batches change: the reason remains
visible, and the human can reject it and review the new run.

Use `python -m pytest tests/test_scheduler.py` for focused tests. Set
`TEST_DATABASE_URL` to a disposable PostgreSQL database to include transactional
queue, concurrency, notice-change and approval tests; they use isolated schemas.

APScheduler references: [AsyncIOScheduler](https://apscheduler.readthedocs.io/en/3.x/modules/schedulers/asyncio.html)
and [user guide](https://apscheduler.readthedocs.io/en/3.x/userguide.html).

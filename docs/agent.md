# Durable agent and minimal CLI

Full implementation: [graph nodes](../app/agents/graph.py),
[audit repository](../app/agents/repository.py), [Postgres runtime](../app/agents/runtime.py),
[verification](../app/services/verification.py), [CLI](../run_agent.py).

The fixed order is `collect -> plan -> retrieve -> match -> approve -> act -> verify`.
`collect` interrupts before retrieval when the household batch is missing.
`plan` records the source registry and brand/strength/batch scope. `retrieve`
fetches and saves notices. `match` saves deterministic decisions and evidence.
Only MATCH decisions with a dated official notice can propose alerts.
`approve` interrupts with destination/message and accepts explicit approval,
rejection, or edits. `act` records an approved mock delivery once. `verify`
reopens the official notice URL independently and retains failures in the audit.

```powershell
.venv\Scripts\python -m scripts.init_db
.venv\Scripts\python run_agent.py start --thread-id synthetic-demo-1 --name ibuprofen --strength '200 mg'
.venv\Scripts\python run_agent.py status --thread-id synthetic-demo-1
```

The omitted batch triggers a question. Save the human answer as `reply.json`:

```json
{"batch_no": "SYN001"}
```

```powershell
.venv\Scripts\python run_agent.py resume --thread-id synthetic-demo-1 --reply-file reply.json
```

If a MATCH produces an approval interrupt, inspect its proposed message and
copy its action ID into a new reply. Include all displayed actions, for example:

```json
{"approvals": [{"action_id": "COPY_DISPLAYED_UUID", "approved": false}]}
```

Change `approved` to `true` only after reviewing it. Optional `destination` and
`message` edits must preserve the notice URL and date. Resume using the same
command and thread ID. No match means no approval question. Live searches using
synthetic batches normally have no matching notices; tests mock official source
responses to exercise all branches without making fictitious public recall claims.

`status` shows durable state, pending interrupts, and errors. `retry` re-enters a
failed node using the same checkpoint; it cannot replace a required human reply.
Each command opens a new PostgresSaver connection and uses the same thread ID.
Repeated steps reuse stable decision IDs and unique action keys. The key is
`medicine_id:internal_notice_id:source_status:content_fingerprint`; action approval/delivery status
does not alter it. The LLM never decides or supplies the verdict.

## LangSmith

Set these values in `.env` to opt into LangGraph tracing:

```dotenv
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=your_key
LANGSMITH_PROJECT=drug-recall-monitor
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
```

The runtime loads `.env` for tracing. LangGraph records nodes, interrupts, and
resumes automatically, with run/thread metadata. Traces include workflow inputs
and evidence; this project uses synthetic households. Tracing remains off by
default. Groq extraction is opt-in for uncertain notices; see
[Render configuration and deployment](groq-deployment.md). Groq extracts public
notice fields only, and the deterministic matcher still supplies every verdict.

References: [LangGraph interrupts](https://reference.langchain.com/python/langgraph/types/interrupt)
and [PostgreSQL checkpointer](https://pypi.org/project/langgraph-checkpoint-postgres/).

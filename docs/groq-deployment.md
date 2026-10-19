# Enable Groq extraction on Render

Push the changes in `app/core/config.py`, `app/agents/graph.py`,
`app/services/notice_matching.py` and `frontend/views/approvals.py` to GitHub.
The extractor SDK is already in `requirements.txt`; no new package is needed.

In the Render dashboard, open `drug-recall-api`, then **Environment**.
Add these values and save with a deployment:

```dotenv
GROQ_EXTRACTION_ENABLED=true
GROQ_API_KEY=YOUR_PRIVATE_GROQ_KEY
GROQ_MODEL=YOUR_AVAILABLE_JSON_MODE_MODEL_ID
GROQ_MIN_CONFIDENCE=0.9
```

Create a key in the [Groq console](https://console.groq.com/keys).
Choose a JSON-mode model available to your account from the
[current model list](https://console.groq.com/docs/models).
Store the actual key only in Render's environment settings. Do not add it to
GitHub, `.env.example`, Streamlit secrets, screenshots or chat messages.

Wait for Render to deploy the new commit and configuration. If automatic deploys
are disabled, select **Manual Deploy > Deploy latest commit**. A restart of an
older commit alone does not install this integration. Streamlit Community Cloud
also needs the latest commit to show the optional extraction captions; its
`DRUG_MONITOR_API_URL` setting stays the same.

Start a **new** check. Runs already paused for confirmation retain their saved
decisions and are not silently recalculated. Only notices that the raw parser
classifies as UNCERTAIN use Groq. Clear MATCH/NO_MATCH decisions require no LLM
call. Missing household batches still interrupt before retrieval or extraction.

Render logs include `GROQ_EXTRACTION_STARTED` and `GROQ_EXTRACTION_COMPLETED`
without API keys, household details or raw notice text. Review cards show when
extraction passed validation or was unusable. Decision evidence stores the
extraction status, model and accepted structured fields. These indicate what
actually happened; a configured key alone is not proof that a call was needed.

Groq receives only the official product description and `code_info`. Fields must
pass Pydantic validation, the confidence threshold and literal source checks.
Python rules compute the verdict. Failed extraction remains UNCERTAIN. Missing
strengths, ambiguous product strengths, batch ranges and genuinely absent batch
lists remain UNCERTAIN even with Groq enabled. The separate verification step
reopens the notice and may still flag unsupported evidence as a visible failure.
Decision confirmation and alert approval remain required.

Set `GROQ_EXTRACTION_ENABLED=false` to restore parser-only behavior for new checks.

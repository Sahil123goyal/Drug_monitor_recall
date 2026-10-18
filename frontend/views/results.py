"""Per-medicine outcomes with official links and preserved uncertainty."""

from uuid import UUID

import streamlit as st

from frontend.api_client import APIError
from frontend.presentation import medicine_result, official_url, snapshot_label
from frontend.ui import client, load_run, selected_run


@st.fragment(run_every="2s")
def show_results(identifier: UUID) -> None:
    run = load_run(identifier)
    if run is None:
        return
    try:
        inventory = client().medicines(run.household_id)
    except APIError:
        inventory = []
    st.caption(f"Check {str(run.id)[:8]} · {run.status.title()} · {run.created_at:%d %b %Y %H:%M UTC}")
    if run.error:
        st.error(run.error)
    for medicine_id in run.medicine_ids:
        result = medicine_result(run, medicine_id)
        with st.container(border=True):
            st.subheader(snapshot_label(run, medicine_id, inventory))
            getattr(st, result.tone)(result.label + " · " + result.detail)
            if result.uncertain:
                st.warning("UNCERTAIN item: at least one notice lacks enough evidence for a verdict.")
            for action in result.actions:
                url = official_url(action.notice_url)
                st.caption(f"Alert status: {action.status.value} · Notice date: {action.notice_date or 'missing'}")
                if url:
                    st.link_button("Open official notice", url)
                else:
                    st.error("Official notice link unavailable.")
            state = run.state.get(str(medicine_id), {})
            for verification in state.get("verification_results", []):
                text = f"Verification: {verification['result']} — {verification['reason']}"
                if verification["result"] == "CONFIRMED":
                    st.caption(text)
                else:
                    st.error(text)
            for failure in state.get("failures", []):
                st.error(f"{failure['node'].title()}: {failure['reason']}")
            with st.expander("Decisions and evidence"):
                decisions = [item for item in run.decisions if item.medicine_id == medicine_id]
                if not decisions:
                    st.caption("No notice decisions were recorded for this medicine.")
                for decision in decisions:
                    st.write(f"{decision.verdict.value}: {decision.reason}")
                    st.json(decision.evidence, expanded=False)


st.title("Results")
st.caption("Outcomes refer to the medicine details saved at the time of the check.")
identifier = selected_run()
if identifier:
    show_results(identifier)

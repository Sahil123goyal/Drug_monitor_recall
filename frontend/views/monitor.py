"""Start household runs and poll real node completion state."""

from uuid import UUID

import streamlit as st

from frontend.api_client import APIError
from frontend.presentation import snapshot_label, step_rows
from frontend.ui import client, household_id, load_run, selected_run


@st.fragment(run_every="2s")
def live_progress(identifier: UUID) -> None:
    run = load_run(identifier)
    if run is None:
        return
    try:
        medicines = client().medicines(run.household_id)
    except APIError:
        medicines = []
    a, b, c = st.columns(3)
    a.metric("Check status", run.status.title())
    b.metric("Medicines finished", f"{run.cursor} / {len(run.medicine_ids)}")
    c.metric("Decisions recorded", len(run.decisions))
    if run.pending_interrupt:
        needs_batch = run.pending_interrupt.value.get("kind") == "collect"
        st.warning("A batch number is needed." if needs_batch else "An alert needs your approval.")
        st.page_link("views/approvals.py", label="Open Approvals", icon="✋")
    if run.error:
        st.error(run.error)
    st.caption("Refreshes every 2 seconds. Steps update when the agent saves progress.")
    for medicine_id in run.medicine_ids:
        with st.container(border=True):
            st.subheader(snapshot_label(run, medicine_id, medicines))
            rows = step_rows(run, medicine_id)
            for column, row in zip(st.columns(7), rows):
                column.caption(row["Step"])
                column.write(row["Status"])
            state = run.state.get(str(medicine_id), {})
            if state.get("plan"):
                st.caption("Sources: " + ", ".join(state["plan"]["sources"]) + " · Scope: brand + strength + batch")
            for failure in state.get("failures", []):
                st.error(f"{failure['node'].title()}: {failure['reason']}")
    if run.status in {"COMPLETED", "FAILED"}:
        st.page_link("views/results.py", label="View results and evidence", icon="📋")


def render() -> None:
    st.title("Run monitor")
    st.caption("Check the current household inventory against official openFDA recall notices.")
    with st.form("start_check"):
        destination = st.text_input("Alert destination", value="mock://synthetic-household", max_chars=320)
        submitted = st.form_submit_button("Start check", type="primary")
    if submitted:
        if not destination.strip():
            st.error("Enter an alert destination.")
        else:
            try:
                run = client().start_run(household_id(), destination)
                st.session_state["preferred_run"] = str(run.id)
                st.session_state["flash"] = "Check started. Any missing batch or proposed alert will appear in Approvals."
                st.rerun()
            except APIError as exc:
                st.error(str(exc))
    identifier = selected_run()
    if identifier:
        live_progress(identifier)


render()

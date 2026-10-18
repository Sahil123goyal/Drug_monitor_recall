"""Explicit human responses, scoped to the currently displayed interrupt."""

from uuid import UUID

import streamlit as st

from app.schemas.api import RunResponse
from frontend.api_client import APIError
from frontend.presentation import official_url
from frontend.ui import client, flash, load_run, selected_run


def choose(run: RunResponse, action_id: str, approved: bool) -> None:
    question = run.pending_interrupt
    assert question is not None
    key = f"choices:{run.id}:{question.id}"
    choices = dict(st.session_state.get(key, {}))
    choices[action_id] = approved
    st.session_state[key] = choices
    expected = {item["action_id"] for item in question.value["alerts"]}
    if set(choices) == expected:
        try:
            client().approve(run.id, question.id, choices)
        except APIError as exc:
            st.error(str(exc))
            return  # Never automatically retry a possibly accepted submission.
        del st.session_state[key]
        flash("Your review was submitted. The agent will continue with your choices.")
    else:
        st.rerun()


def choose_decision(run: RunResponse, decision_id: str, accepted: bool) -> None:
    question = run.pending_interrupt
    assert question is not None
    key = f"confirm:{run.id}:{question.id}"
    choices = dict(st.session_state.get(key, {}))
    choices[decision_id] = accepted
    st.session_state[key] = choices
    expected = {item["decision_id"] for item in question.value["decisions"]}
    if set(choices) == expected:
        try:
            client().confirm_decisions(run.id, question.id, choices)
        except APIError as exc:
            st.error(str(exc))
            return
        del st.session_state[key]
        flash("Decision review submitted. The check will continue with accepted verdicts.")
    else:
        st.rerun()


@st.fragment(run_every="2s")
def pending_review(identifier: UUID) -> None:
    run = load_run(identifier)
    if run is None:
        return
    question = run.pending_interrupt
    if run.status != "WAITING" or question is None:
        st.info("No input is currently requested for this check.")
        if run.error:
            st.error(run.error)
        st.caption(f"Check status: {run.status.title()}. This page refreshes automatically.")
        return
    if question.value.get("kind") == "collect":
        st.warning("Batch number required")
        st.write(question.value.get("question", "What batch number is printed on your medicine package?"))
        try:
            medicine = next((item for item in client().medicines(run.household_id) if item.id == question.medicine_id), None)
            if medicine:
                st.subheader(f"{medicine.name} · {medicine.strength}")
        except APIError:
            pass
        with st.form(f"batch:{run.id}:{question.id}"):
            batch = st.text_input("Batch number", placeholder="Enter the batch exactly as printed", max_chars=200)
            submitted = st.form_submit_button("Provide batch", type="primary")
        if submitted:
            if not batch.strip():
                st.error("Enter a batch number to continue.")
            else:
                try:
                    client().provide_batch(run.id, question.id, batch)
                    flash("Batch supplied. The check will continue.")
                except APIError as exc:
                    st.error(str(exc))
        return
    if question.value.get("kind") == "confirm":
        st.caption("Review every matcher decision. Choices are submitted together once each decision has an Accept or Reject.")
        choices = st.session_state.get(f"confirm:{run.id}:{question.id}", {})
        decisions = question.value["decisions"]
        for item in decisions:
            decision_id = item["decision_id"]
            url = official_url(item.get("notice_url"))
            with st.container(border=True):
                st.subheader(item.get("verdict", "Decision"))
                st.write(item.get("reason") or "No reason recorded.")
                if item.get("notice_date"):
                    st.caption(f"Notice date: {item['notice_date']}")
                if url:
                    st.link_button("Open official notice", url)
                else:
                    st.caption("Official notice link unavailable for this decision.")
                if decision_id in choices:
                    st.info("Selected: " + ("Accept" if choices[decision_id] else "Reject"))
                accept, reject = st.columns(2)
                if accept.button("Accept", key=f"accept:{question.id}:{decision_id}", type="primary",
                                 use_container_width=True):
                    choose_decision(run, decision_id, True)
                if reject.button("Reject", key=f"reject-decision:{question.id}:{decision_id}",
                                 use_container_width=True):
                    choose_decision(run, decision_id, False)
        st.caption(f"{len(choices)} of {len(decisions)} decisions reviewed. Alerts are not proposed until review is complete.")
        return
    if question.value.get("kind") != "approve":
        st.error("This input request is not supported by this UI.")
        return
    st.caption("Review every proposed alert. Choices are submitted together once each alert has an Approve or Reject decision.")
    actions = {str(item.id): item for item in run.actions}
    choices = st.session_state.get(f"choices:{run.id}:{question.id}", {})
    alerts = question.value["alerts"]
    for alert in alerts:
        action_id = alert["action_id"]
        action = actions.get(action_id)
        url = official_url(action.notice_url) if action else None
        with st.container(border=True):
            st.subheader("Proposed mock alert")
            st.text(alert["message"])
            st.caption(f"Destination: {alert['destination']}")
            if url and action and action.notice_date:
                st.write(f"Notice date: {action.notice_date.isoformat()}")
                st.link_button("Open official notice", url)
            else:
                st.error("The official notice link or date is missing. Approval is unavailable.")
            if action_id in choices:
                st.info("Selected: " + ("Approve" if choices[action_id] else "Reject"))
            approve, reject = st.columns(2)
            if approve.button("Approve", key=f"approve:{question.id}:{action_id}", type="primary",
                              disabled=not (url and action and action.notice_date), use_container_width=True):
                choose(run, action_id, True)
            if reject.button("Reject", key=f"reject:{question.id}:{action_id}", use_container_width=True):
                choose(run, action_id, False)
    st.caption(f"{len(choices)} of {len(alerts)} alerts reviewed. No alert is sent while review is incomplete.")


st.title("Approvals")
st.caption("Provide missing batches, confirm matcher decisions, and decide whether each proposed alert should be sent.")
identifier = selected_run()
if identifier:
    pending_review(identifier)

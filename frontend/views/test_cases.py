"""Inspect and execute the project's four official acceptance cases."""

import json

import streamlit as st

from app.services.official_cases import OFFICIAL_CASES
from frontend.api_client import APIError
from frontend.ui import client


def render() -> None:
    st.title("Test cases")
    st.caption("Official project cases · synthetic household · real FDA record replay for TC1/TC2 · clearly labelled status fixture for TC3.")
    st.info("The UI uses a recorded FDA response. Dated alerts and status updates are inspected at the human-approval pause; no alerts are sent.")
    api = client()
    try:
        records = api.test_runs()
    except APIError as exc:
        st.error(str(exc))
        records = []
    if st.button("Run TC1–TC4", type="primary"):
        try:
            records = api.evaluate()
            if len(records) == 4 and all(item.passed is True for item in records):
                st.success("All four cases passed and were saved.")
            else:
                st.warning("Results saved. Review the explanation for every failed or incomplete case.")
        except APIError as exc:
            st.error(str(exc))
    by_name = {item.tc_name: item for item in records}
    rows = []
    for case in OFFICIAL_CASES:
        saved = by_name.get(case.name)
        rows.append({"Case": case.name, "Scenario": case.description,
                     "Input": json.dumps(saved.input if saved else case.inputs()),
                     "Expected": json.dumps(saved.expected if saved else case.expected()),
                     "Actual": json.dumps(saved.actual) if saved and saved.actual is not None else "Not run",
                     "Evidence": json.dumps(saved.evidence) if saved and saved.evidence is not None else "Not recorded",
                     "Result": "Not run" if not saved or saved.passed is None else ("PASS" if saved.passed else "FAIL")})
    st.dataframe(rows, hide_index=True, use_container_width=True)
    for case in OFFICIAL_CASES:
        saved = by_name.get(case.name)
        outcome = "Not run" if not saved or saved.passed is None else ("PASS" if saved.passed else "FAIL")
        with st.expander(f"{case.name} · {case.description} · {outcome}"):
            if case.name == "TC3":
                st.warning("FIXTURE ONLY: simulated Terminated status. This is not an observed withdrawal by FDA.")
            if saved and saved.passed is False:
                st.error((saved.evidence or {}).get("explanation", "Actual behavior differed from the expectation."))
            left, right = st.columns(2)
            left.write("Input")
            left.json(saved.input if saved else case.inputs())
            right.write("Expected")
            right.json(saved.expected if saved else case.expected())
            right.write("Actual")
            if saved and saved.actual is not None:
                right.json(saved.actual)
            else:
                right.caption("Not evaluated yet.")
            st.write("Evidence")
            if saved and saved.evidence is not None:
                st.json(saved.evidence)
            else:
                st.caption("Run the cases to record evidence.")


render()

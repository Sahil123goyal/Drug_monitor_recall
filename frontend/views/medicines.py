"""Household inventory forms."""

import streamlit as st

from frontend.api_client import APIError
from frontend.presentation import medicine_label
from frontend.ui import client, flash, household_id


def render() -> None:
    st.title("Medicine list")
    st.caption("Use the brand, strength, and batch printed on the package. A missing batch will pause a check.")
    household = household_id()
    api = client()
    loaded = True
    try:
        medicines = api.medicines(household)
    except APIError as exc:
        st.error(str(exc))
        medicines = []
        loaded = False
    total, missing = st.columns(2)
    total.metric("Medicines", len(medicines) if loaded else "—")
    missing.metric("Missing batch", sum(item.batch_no is None for item in medicines) if loaded else "—")
    if medicines:
        st.dataframe([{"Brand": item.name, "Strength": item.strength,
                       "Batch": item.batch_no or "Missing — input required"} for item in medicines],
                     hide_index=True, use_container_width=True)
    elif loaded:
        st.info("Add your first synthetic medicine below.")

    add, edit = st.tabs(["Add medicine", "Edit medicine"])
    with add:
        with st.form("add_medicine", clear_on_submit=False):
            name = st.text_input("Brand", placeholder="DemoRelief", max_chars=200)
            strength = st.text_input("Strength", placeholder="500 mg", max_chars=100)
            batch = st.text_input("Batch number (optional)", placeholder="SYN001", max_chars=200)
            submitted = st.form_submit_button("Add medicine", type="primary")
        if submitted:
            if not name.strip() or not strength.strip():
                st.error("Brand and strength are required.")
            else:
                try:
                    api.save_medicine(household, name, strength, batch)
                    flash("Medicine added.")
                except APIError as exc:
                    st.error(str(exc))
    with edit:
        if not medicines:
            st.caption("There are no medicines to edit yet.")
            return
        by_id = {str(item.id): item for item in medicines}
        selected = st.selectbox("Medicine to edit", list(by_id),
                                format_func=lambda value: medicine_label(by_id[value]))
        medicine = by_id[selected]
        with st.form(f"edit:{selected}"):
            name = st.text_input("Brand", value=medicine.name, key=f"edit_name:{selected}", max_chars=200)
            strength = st.text_input("Strength", value=medicine.strength, key=f"edit_strength:{selected}", max_chars=100)
            batch = st.text_input("Batch number (optional)", value=medicine.batch_no or "",
                                  key=f"edit_batch:{selected}", max_chars=200)
            submitted = st.form_submit_button("Save changes", type="primary")
        st.caption("Finish active checks before editing. Supply a requested batch on the Approvals page.")
        if submitted:
            if not name.strip() or not strength.strip():
                st.error("Brand and strength are required.")
            else:
                try:
                    api.save_medicine(household, name, strength, batch, medicine.id)
                    flash("Medicine updated. Start a new check to assess these details.")
                except APIError as exc:
                    st.error(str(exc))


render()

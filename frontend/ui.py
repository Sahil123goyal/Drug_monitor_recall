"""Small shared UI helpers; no side effects during polling or navigation."""

from uuid import UUID

import streamlit as st

from app.schemas.api import RunResponse
from frontend.api_client import APIClient, APIError


def client() -> APIClient:
    return APIClient()


def household_id() -> UUID:
    try:
        return UUID(st.session_state.get("household_id", "00000000-0000-4000-8000-000000000001"))
    except ValueError:
        st.error("Enter a valid household UUID in the sidebar.")
        st.stop()


def selected_run() -> UUID | None:
    household = household_id()
    try:
        runs = client().runs(household)
    except APIError as exc:
        st.error(str(exc))
        return None
    if not runs:
        st.info("No checks yet. Start one from Run monitor.")
        return None
    key = f"selected_run:{household}"
    options = [str(item.id) for item in runs]
    labels = {str(item.id): f"{item.created_at:%d %b %H:%M} · {item.status.title()} · {str(item.id)[:8]}" for item in runs}
    preferred = st.session_state.pop("preferred_run", None)
    if preferred in options:
        st.session_state[key] = preferred
    if st.session_state.get(key) not in options:
        st.session_state[key] = options[0]
    chosen = st.selectbox("Check", options, format_func=labels.get, key=key)
    return UUID(chosen)


def load_run(identifier: UUID) -> RunResponse | None:
    try:
        result = client().run(identifier)
    except APIError as exc:
        st.error(str(exc))
        return None
    if result.household_id != household_id():
        st.error("This check belongs to a different household.")
        return None
    return result


def flash(message: str) -> None:
    st.session_state["flash"] = message
    st.rerun()


def show_flash() -> None:
    message = st.session_state.pop("flash", None)
    if message:
        st.success(message)

"""Five-page Streamlit application. Run: python -m streamlit run frontend/app.py."""

import sys
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

# Streamlit can put frontend/ rather than the project root on sys.path.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) in sys.path:
    sys.path.remove(str(ROOT))
sys.path.insert(0, str(ROOT))  # Must precede frontend/app.py when importing the app package.
load_dotenv(ROOT / ".env", override=False)

from frontend.ui import show_flash

st.set_page_config(page_title="Drug Recall Monitor", page_icon="💊", layout="wide")
st.markdown("""<style>
    .block-container {max-width: 1180px; padding-top: 2.5rem;}
    [data-testid="stSidebar"] {background: #edf5f2;}
    h1, h2, h3 {letter-spacing: -.025em;}
    [data-testid="stMetric"] {background: #f2f7f5; border-radius: 12px; padding: 16px;}
    [data-testid="stMetricValue"] {color: #126854;}
    .stButton > button[kind="primary"] {background: #126854; border-color: #126854;}
</style>""", unsafe_allow_html=True)

with st.sidebar:
    st.title("💊 Drug Recall Monitor")
    st.caption("A precise check for your household medicines.")
    st.text_input("Household ID", value="00000000-0000-4000-8000-000000000001", key="household_id")
    st.caption("Synthetic household demo · mock alerts")
    st.divider()

navigation = st.navigation([
    st.Page("views/medicines.py", title="Medicine list", icon="💊", default=True),
    st.Page("views/monitor.py", title="Run monitor", icon="▶️"),
    st.Page("views/approvals.py", title="Approvals", icon="✋"),
    st.Page("views/results.py", title="Results", icon="📋"),
    st.Page("views/test_cases.py", title="Test cases", icon="🧪"),
])
show_flash()
navigation.run()


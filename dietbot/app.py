#!/usr/bin/env python3
"""
app.py – Streamlit UI for DietBot (ChatGPT‑style)
"""

import streamlit as st
from recommender import generate_recommendation
from session_manager import ChatSessionManager

# Initialize session manager
mgr = ChatSessionManager()

# Page config
st.set_page_config(page_title="DietBot", page_icon="🥗", layout="wide")

# --------------------------------------------
# 1. Initialize session state
# --------------------------------------------
if "chat_started" not in st.session_state:
    st.session_state.chat_started = False
if "patient_id" not in st.session_state:
    st.session_state.patient_id = ""
if "messages" not in st.session_state:
    st.session_state.messages = []
if "current_session_id" not in st.session_state:
    st.session_state.current_session_id = None

# --------------------------------------------
# 2. Patient ID entry screen
# --------------------------------------------
if not st.session_state.chat_started:
    st.title("🥗 Welcome to DietBot")
    st.markdown("Please enter your Patient ID to continue.")
    with st.form(key="patient_form", clear_on_submit=False):
        pid = st.text_input("Patient ID", placeholder="e.g., 398c2344-975e-44ad-b483-d1375e1376c0")
        submitted = st.form_submit_button("Start Chat")
        if submitted and pid.strip():
            st.session_state.patient_id = pid.strip()
            st.session_state.chat_started = True
            st.session_state.current_session_id = None
            st.session_state.messages = []
            st.rerun()
    st.stop()

# --------------------------------------------
# 3. Main chat interface
# --------------------------------------------
patient_id = st.session_state.patient_id

# Sidebar
with st.sidebar:
    st.title("🥗 DietBot")
    st.markdown(f"**Patient:** `{patient_id}`")
    st.markdown("---")

    st.subheader("📋 Sessions (max 5)")

    # List sessions for this patient
    sessions = mgr.list_sessions(patient_id)

    if sessions:
        for s in sessions:
            col1, col2 = st.columns([4, 1])
            with col1:
                # Display session title with arrow if it's the current one
                title = s["title"]
                if s["id"] == st.session_state.get("current_session_id"):
                    title = f"👉 {title}"
                if st.button(title, key=f"session_{s['id']}"):
                    st.session_state.current_session_id = s["id"]
                    st.session_state.messages = mgr.get_session_history(s["id"], limit=100)
                    st.rerun()
            with col2:
                if st.button("✕", key=f"del_{s['id']}"):
                    mgr.delete_session(s["id"])
                    if st.session_state.get("current_session_id") == s["id"]:
                        st.session_state.current_session_id = None
                        st.session_state.messages = []
                    st.rerun()
    else:
        st.info("No sessions yet.")

    if st.button("➕ New Chat"):
        st.session_state.current_session_id = None
        st.session_state.messages = []
        st.rerun()

    st.markdown("---")
    if st.button("🔄 Change Patient"):
        st.session_state.chat_started = False
        st.session_state.patient_id = ""
        st.session_state.current_session_id = None
        st.session_state.messages = []
        st.rerun()

# Main chat area
st.title("💬 DietBot")

# Display conversation history
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# Chat input
if prompt := st.chat_input("Ask about diet..."):
    # Add user message to UI
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # Generate assistant response
    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            try:
                result = generate_recommendation(
                    patient_id,
                    prompt,
                    session_id=st.session_state.current_session_id
                )
                # Update current session ID (may be new)
                st.session_state.current_session_id = result["session_id"]
                # Refresh full history from DB
                full_history = mgr.get_session_history(result["session_id"], limit=100)
                st.session_state.messages = full_history
                # Display the latest assistant message
                if full_history:
                    last_msg = full_history[-1]
                    if last_msg["role"] == "assistant":
                        st.markdown(last_msg["content"])
            except Exception as e:
                st.error(f"Error: {e}")
                st.session_state.messages.append({"role": "assistant", "content": f"Sorry, an error occurred: {e}"})
    st.rerun()
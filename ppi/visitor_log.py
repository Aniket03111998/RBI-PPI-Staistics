"""Emails the name/LinkedIn a viewer submits at the gate, via Web3Forms.

Deliberately not the SQLite db in data/ — that file gets committed to the public GitHub
repo by the auto-refresh workflow, which would publish visitor PII. Web3Forms just relays
the submission to a private inbox; nothing visitor-related touches git.

The write is best-effort: a Web3Forms outage or missing access key must never block a
visitor from reaching the dashboard after they've filled the form.
"""
from __future__ import annotations

import requests
import streamlit as st


def log_visitor(name: str, linkedin_url: str) -> None:
    try:
        access_key = st.secrets["web3forms_access_key"]
        requests.post(
            "https://api.web3forms.com/submit",
            json={
                "access_key": access_key,
                "subject": "New dashboard visitor",
                "name": name,
                "linkedin_url": linkedin_url,
            },
            timeout=5,
        )
    except Exception as e:
        # ponytail: swallow-and-log, dashboard access must not depend on the relay being up.
        st.session_state["_visitor_log_error"] = str(e)

"""Session TTL for Streamlit (Wall-clock; clears session_state on expiry)."""

from __future__ import annotations

import os
import time

# Default 8 hours
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", "28800"))


def session_expired(login_ts: float | None) -> bool:
    if login_ts is None:
        return True
    return (time.time() - float(login_ts)) > SESSION_TTL_SECONDS

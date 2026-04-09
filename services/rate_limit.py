"""Simple client-side throttle for LLM calls (per-process)."""

from __future__ import annotations

import os
import time
from threading import Lock

_lock = Lock()
_last_ts: float = 0.0
_MIN_INTERVAL = float(os.environ.get("LLM_MIN_INTERVAL_SECONDS", "1.25"))


def throttle_llm() -> None:
    """Sleep if needed so consecutive API calls are spaced."""
    global _last_ts
    with _lock:
        now = time.time()
        wait = _MIN_INTERVAL - (now - _last_ts)
        if wait > 0:
            time.sleep(wait)
        _last_ts = time.time()

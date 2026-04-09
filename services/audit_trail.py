"""Append-only JSONL audit trail (who ran what, when)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = Path(os.environ.get("CLEARSPEND_AUDIT_LOG", ROOT / "data" / "audit_trail.jsonl"))


def log_event(
    *,
    username: str,
    action: str,
    detail: dict | None = None,
) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.time(),
        "username": username,
        "action": action,
        "detail": detail or {},
    }
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")

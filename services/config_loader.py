"""Ignored vendors + FX rates from JSON config (server-side)."""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IGNORED_VENDORS_PATH = Path(
    os.environ.get("CLEARSPEND_IGNORED_VENDORS", ROOT / "config" / "ignored_vendors.json")
)
FX_RATES_PATH = Path(os.environ.get("CLEARSPEND_FX_RATES", ROOT / "config" / "fx_rates.json"))


def load_ignored_vendors() -> set[str]:
    if not IGNORED_VENDORS_PATH.is_file():
        return set()
    try:
        with open(IGNORED_VENDORS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return {str(x).strip().lower() for x in data if x}
        if isinstance(data, dict) and "vendors" in data:
            return {str(x).strip().lower() for x in data["vendors"] if x}
    except (json.JSONDecodeError, OSError):
        pass
    return set()


def load_fx_rates() -> dict[str, float]:
    """Per-unit currency → USD multiplier (e.g. EUR: 1.08 means 1 EUR = 1.08 USD)."""
    if not FX_RATES_PATH.is_file():
        return {}
    try:
        with open(FX_RATES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        out: dict[str, float] = {}
        for k, v in data.items():
            try:
                out[str(k).strip().upper()] = float(v)
            except (TypeError, ValueError):
                continue
        return out
    except (json.JSONDecodeError, OSError):
        return {}

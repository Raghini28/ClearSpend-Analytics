"""Safe file ingest + sanity limits (no silent crashes on bad uploads)."""

from __future__ import annotations

import io
import os
from typing import Any

import pandas as pd

MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))  # 50 MB
MAX_ROWS = int(os.environ.get("MAX_LEDGER_ROWS", "2000000"))


class UploadValidationError(Exception):
    pass


def read_ledger_bytes(
    raw: bytes,
    filename: str,
    *,
    max_bytes: int | None = None,
    max_rows: int | None = None,
) -> pd.DataFrame:
    max_bytes = max_bytes or MAX_UPLOAD_BYTES
    max_rows = max_rows or MAX_ROWS
    if len(raw) > max_bytes:
        raise UploadValidationError(
            f"File too large ({len(raw)} bytes). Limit {max_bytes} bytes."
        )
    lower = (filename or "").lower()
    try:
        if lower.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(raw))
        elif lower.endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(raw))
        else:
            raise UploadValidationError("Only .csv, .xlsx, .xls are supported.")
    except UploadValidationError:
        raise
    except Exception as e:
        raise UploadValidationError(
            f"Could not parse file ({type(e).__name__}): {e}"
        ) from e
    if df is None or df.empty:
        raise UploadValidationError("File has no rows.")
    if len(df) > max_rows:
        raise UploadValidationError(
            f"Too many rows ({len(df)}). Limit {max_rows}. Slice the export or raise MAX_LEDGER_ROWS."
        )
    if len(df.columns) == 0:
        raise UploadValidationError("No columns found.")
    return df


def validate_prepared_context(ctx: dict[str, Any]) -> list[str]:
    """Post-prepare warnings for schema quality (non-fatal)."""
    msgs: list[str] = []
    if ctx.get("error"):
        return msgs
    df = ctx.get("df")
    if df is not None and len(df) < 3:
        msgs.append("Very few rows — statistical checks may be weak.")
    return msgs

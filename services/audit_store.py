"""SQLite persistence for audit runs (survives page reload)."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("CLEARSPEND_AUDIT_DB", ROOT / "data" / "audits.sqlite"))


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS audits (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                created REAL NOT NULL,
                file_label TEXT,
                provider TEXT,
                summary_json TEXT,
                report_md TEXT,
                row_count INTEGER,
                total_exposure REAL
            )
            """
        )
        c.commit()


def save_audit(
    username: str,
    file_label: str,
    provider: str,
    summary_df_json: str,
    report_md: str,
    row_count: int,
    total_exposure: float,
) -> str:
    init_db()
    aid = str(uuid.uuid4())
    with _conn() as c:
        c.execute(
            """INSERT INTO audits
            (id, username, created, file_label, provider, summary_json, report_md, row_count, total_exposure)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                aid,
                username,
                time.time(),
                file_label,
                provider,
                summary_df_json,
                report_md,
                row_count,
                total_exposure,
            ),
        )
        c.commit()
    return aid


def list_audits(
    username: str, limit: int = 50, *, is_admin: bool = False
) -> list[dict[str, Any]]:
    init_db()
    with _conn() as c:
        if is_admin:
            cur = c.execute(
                """SELECT id, username, created, file_label, provider, row_count, total_exposure,
                          length(report_md) AS report_len
                   FROM audits ORDER BY created DESC LIMIT ?""",
                (limit,),
            )
        else:
            cur = c.execute(
                """SELECT id, username, created, file_label, provider, row_count, total_exposure,
                          length(report_md) AS report_len
                   FROM audits WHERE username = ? ORDER BY created DESC LIMIT ?""",
                (username, limit),
            )
        return [dict(r) for r in cur.fetchall()]


def get_audit(
    audit_id: str, username: str, *, is_admin: bool = False
) -> dict[str, Any] | None:
    init_db()
    with _conn() as c:
        if is_admin:
            cur = c.execute("SELECT * FROM audits WHERE id = ?", (audit_id,))
        else:
            cur = c.execute(
                "SELECT * FROM audits WHERE id = ? AND username = ?",
                (audit_id, username),
            )
        row = cur.fetchone()
        return dict(row) if row else None


def delete_audit(
    audit_id: str, username: str, *, is_admin: bool = False
) -> bool:
    init_db()
    with _conn() as c:
        if is_admin:
            cur = c.execute("DELETE FROM audits WHERE id = ?", (audit_id,))
        else:
            cur = c.execute(
                "DELETE FROM audits WHERE id = ? AND username = ?",
                (audit_id, username),
            )
        c.commit()
        return cur.rowcount > 0

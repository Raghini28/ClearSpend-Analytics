"""PDF + optional email export for audit summaries."""

from __future__ import annotations

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from io import BytesIO
from typing import Any

import pandas as pd

try:
    from fpdf import FPDF
except ImportError:
    FPDF = None  # type: ignore


def build_pdf_bytes(title: str, summary_md: str, summary_df: pd.DataFrame | None) -> bytes | None:
    if FPDF is None:
        return None
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.multi_cell(0, 8, title)
    pdf.ln(4)
    pdf.set_font("Helvetica", "", 10)
    if summary_df is not None and not summary_df.empty:
        pdf.multi_cell(0, 6, "Summary (CSV-style lines)")
        pdf.ln(2)
        hdr = " | ".join(str(c) for c in summary_df.columns)
        pdf.set_font("Helvetica", "B", 9)
        pdf.multi_cell(0, 5, hdr[:2000])
        pdf.set_font("Helvetica", "", 8)
        for _, r in summary_df.head(60).iterrows():
            line = " | ".join(str(x)[:48] for x in r.tolist())
            pdf.multi_cell(0, 5, line[:2000])
    pdf.ln(4)
    pdf.set_font("Helvetica", "", 9)
    body = (summary_md or "No narrative.")[:12000]
    for para in body.split("\n\n"):
        pdf.multi_cell(0, 5, para.replace("\n", " ")[:4000])
        pdf.ln(2)
    out = pdf.output(dest="S")
    if isinstance(out, str):
        return out.encode("latin-1", errors="replace")
    return bytes(out) if out is not None else None


def send_audit_email(
    subject: str,
    body: str,
    *,
    secrets_get: Any,
) -> tuple[bool, str]:
    host = os.environ.get("SMTP_HOST") or _sec(secrets_get, "SMTP_HOST")
    if not host:
        return False, "SMTP not configured (set SMTP_HOST or secrets)."
    port = int(os.environ.get("SMTP_PORT") or _sec(secrets_get, "SMTP_PORT") or "587")
    user = os.environ.get("SMTP_USER") or _sec(secrets_get, "SMTP_USER")
    pwd = os.environ.get("SMTP_PASSWORD") or _sec(secrets_get, "SMTP_PASSWORD")
    from_addr = os.environ.get("SMTP_FROM") or _sec(secrets_get, "SMTP_FROM") or user
    to_addr = os.environ.get("SMTP_TO") or _sec(secrets_get, "SMTP_TO")
    if not to_addr:
        return False, "SMTP_TO not set."
    try:
        msg = MIMEMultipart()
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to_addr
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            if user and pwd:
                s.login(user, pwd)
            s.sendmail(from_addr, [to_addr], msg.as_string())
        return True, "Sent."
    except Exception as e:
        return False, str(e)


def _sec(secrets_get: Any, key: str) -> str:
    try:
        return str(secrets_get(key) or "")
    except Exception:
        return ""

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


def _pdf_core_font_safe(text: str) -> str:
    """Helvetica only supports latin-1; strip/replace emoji and other Unicode."""
    if not text:
        return ""
    return (text or "").encode("latin-1", errors="replace").decode("latin-1")


def clean_pdf_text(text: str) -> str:
    """Normalize text for PDF core fonts (alias for Latin-1-safe output)."""
    return _pdf_core_font_safe(text)


def safe_text(text: str, limit: int = 2000) -> str:
    text = clean_pdf_text(text)
    return text[:limit]


def build_pdf_bytes(title: str, summary_md: str, summary_df: pd.DataFrame | None) -> bytes | None:
    if FPDF is None:
        return None
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    text_w = float(getattr(pdf, "epw", pdf.w - pdf.r_margin - pdf.l_margin))

    def _mc(line: str, h: float) -> None:
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(text_w, h, line)

    pdf.set_font("Helvetica", "B", 14)
    _mc(_pdf_core_font_safe(title), 8)
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 10)
    if summary_df is not None and not summary_df.empty:
        _mc("Summary (CSV-style lines)", 6)
        pdf.ln(1)
        hdr = " | ".join(_pdf_core_font_safe(str(c)) for c in summary_df.columns)
        pdf.set_font("Helvetica", "B", 9)
        _mc(_pdf_core_font_safe(hdr[:2000]), 5)
        pdf.set_font("Helvetica", "", 8)
        for _, r in summary_df.head(60).iterrows():
            line = " | ".join(
                _pdf_core_font_safe(str(x)[:48]) for x in r.tolist()
            )
            _mc(_pdf_core_font_safe(line[:2000]), 5)
        pdf.ln(2)
    pdf.set_font("Helvetica", "", 9)
    pdf.multi_cell(text_w, 5, "Narrative (line by line):")
    pdf.ln(1)
    for line in (summary_md or "No narrative.").splitlines():
        if not line.strip():
            pdf.ln(2)
            continue
        _mc(safe_text(line, 2000), 5)
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

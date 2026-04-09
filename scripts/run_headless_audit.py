#!/usr/bin/env python3
"""Run Python forensic checks without Streamlit (for cron / scheduled drops)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from audit_engine import (
    deduplicated_flagged_line_exposure,
    ensure_all_checks,
    prepare_ledger,
    summary_table_from_accumulated,
)


def main() -> None:
    p = argparse.ArgumentParser(description="Headless ClearSpend audit")
    p.add_argument("ledger", help="Path to .csv or .xlsx ledger")
    p.add_argument(
        "--out",
        default="",
        help="Write JSON summary to this path (default: stdout)",
    )
    args = p.parse_args()
    path = Path(args.ledger)
    if not path.is_file():
        print(json.dumps({"error": "file_not_found", "path": str(path)}))
        sys.exit(2)
    suf = path.suffix.lower()
    if suf == ".csv":
        df = pd.read_csv(path)
    elif suf in (".xlsx", ".xls"):
        df = pd.read_excel(path)
    else:
        print(json.dumps({"error": "unsupported_type", "suffix": suf}))
        sys.exit(2)
    ctx = prepare_ledger(df)
    if ctx.get("error"):
        print(json.dumps({"error": ctx["error"], "file": str(path)}))
        sys.exit(1)
    ensure_all_checks(ctx)
    table = summary_table_from_accumulated(ctx)
    category_sum = (
        float(table["Amount ($)"].sum()) if table is not None and not table.empty else 0.0
    )
    unique_exp, unique_n = deduplicated_flagged_line_exposure(ctx)
    out = {
        "file": str(path),
        "row_count": len(ctx["df"]),
        "unique_flagged_line_exposure_usd": unique_exp,
        "unique_flagged_line_count": unique_n,
        "category_exposure_sum_usd": category_sum,
        "total_exposure_usd": unique_exp,
        "categories": json.loads(table.to_json(orient="records")) if table is not None and not table.empty else [],
    }
    text = json.dumps(out, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()

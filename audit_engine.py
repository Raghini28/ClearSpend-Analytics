"""Ledger normalization and forensic checks invoked by the agent and UI."""

from __future__ import annotations

import math
import os
import re
from typing import Any

import numpy as np
import pandas as pd


def _norm(s: str) -> str:
    return "".join(ch.lower() for ch in str(s).strip() if ch.isalnum())


def find_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    norm_map = {_norm(c): c for c in df.columns}
    for cand in candidates:
        if _norm(cand) in norm_map:
            return norm_map[_norm(cand)]
    for col in df.columns:
        c_low = col.lower()
        if any(w in c_low for w in ["id", "key", "ref", "num"]):
            return col
    return None


def find_amt_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    norm_map = {_norm(c): c for c in df.columns}
    for cand in candidates:
        if _norm(cand) in norm_map:
            return norm_map[_norm(cand)]
    nums = df.select_dtypes(include=["number"]).columns
    if not nums.empty:
        return str(df[nums].mean().idxmax())
    return None


MAP_KEYS = ("amount_line", "amount_total", "id", "unit", "vendor", "date")


def _match_col(df: pd.DataFrame, name: str | None) -> str | None:
    if not name or not isinstance(name, str):
        return None
    name = name.strip()
    if name in df.columns:
        return name
    return None


def apply_post_prepare_enrichment(ctx: dict[str, Any]) -> None:
    """Load ignore-list, optional FX rates → USD on monetary columns (in-place on ctx['df'])."""
    from services.config_loader import load_fx_rates, load_ignored_vendors

    ctx["ignored_vendors"] = load_ignored_vendors()
    ctx["fx_normalized"] = False
    if ctx.get("error"):
        return
    df = ctx.get("df")
    if df is None or getattr(df, "empty", True):
        return
    rates = load_fx_rates()
    if not rates or "__CCY" not in df.columns:
        return
    ccy = (
        df["__CCY"]
        .astype(str)
        .str.strip()
        .str.upper()
        .replace({"NAN": "", "NONE": ""})
    )

    def rate_for(c: str) -> float:
        if not c or c in ("USD", "US DOLLAR"):
            return 1.0
        return float(rates.get(c, 1.0))

    mult = ccy.map(rate_for).astype(float)
    for col in ("__L", "__T", "__U", "__PO_AMT", "__TAX", "__FR"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0) * mult
    ctx["fx_normalized"] = True


def _row_vendor_key(row: dict) -> str:
    for k in ("__V", "vendor", "Supplier_Nm", "SUPPLIER_KEY"):
        if k in row and row[k] is not None and str(row[k]).strip():
            return str(row[k]).strip().lower()
    return ""


def apply_ignored_vendors_to_result(
    ctx: dict[str, Any], res: dict[str, Any]
) -> dict[str, Any]:
    ign = ctx.get("ignored_vendors") or set()
    if not ign or not isinstance(res, dict) or res.get("error"):
        return res
    cd = res.get("creep_detail")
    if isinstance(cd, list) and cd:
        cd2 = [r for r in cd if str(r.get("vendor", "")).strip().lower() not in ign]
        if len(cd2) != len(cd):
            out = dict(res)
            out["creep_detail"] = cd2
            out["exposure_usd"] = float(
                sum(float(r.get("approx_exposure_usd") or 0) for r in cd2)
            )
            out["flagged_row_count"] = int(
                sum(int(r.get("rows_affected") or 0) for r in cd2)
            )
            out["by_vendor"] = [
                {"vendor": r["vendor"], "exposure_line_amount": r["approx_exposure_usd"]}
                for r in cd2
            ]
            out["flagged_rows"] = cd2[:300]
            return out
        return res

    fr = res.get("flagged_rows")
    if not isinstance(fr, list) or not fr:
        return res
    fr2 = [r for r in fr if _row_vendor_key(r) not in ign]
    if len(fr2) == len(fr):
        return res
    out = dict(res)
    out["flagged_rows"] = fr2
    out["flagged_row_count"] = len(fr2)
    exp = 0.0
    for r in fr2:
        if "__L" in r:
            try:
                exp += abs(float(r["__L"]))
            except (TypeError, ValueError):
                pass
    if exp > 0:
        out["exposure_usd"] = exp
    else:
        old_exp = float(res.get("exposure_usd") or 0)
        old_n = max(len(fr), 1)
        out["exposure_usd"] = old_exp * (len(fr2) / old_n)
    bv = out.get("by_vendor")
    if isinstance(bv, list):
        out["by_vendor"] = [
            x
            for x in bv
            if str(x.get("vendor", "")).strip().lower() not in ign
        ]
    return out


TOOL_CONFIDENCE: dict[str, str] = {
    "check_math_integrity": "High",
    "find_duplicate_invoices": "High",
    "find_duplicate_payment_patterns": "Medium",
    "find_id_vendor_conflict": "High",
    "find_round_amount_signals": "Low",
    "find_near_approval_threshold": "Medium",
    "find_weekend_postings": "Low",
    "find_high_velocity_vendor_days": "Medium",
    "find_near_duplicate_lines": "Medium",
    "find_tax_freight_anomalies": "Medium",
    "find_missing_po_receipt": "Medium",
    "find_line_over_po_open": "High",
    "find_stale_invoices": "Low",
    "find_data_quality_gaps": "Medium",
    "find_currency_mismatch": "Medium",
    "find_benford_anomaly": "Low",
    "find_split_invoice_heuristic": "Low",
    "find_intercompany_keywords": "Low",
    "detect_price_creep": "Medium",
    "find_negative_leaks": "High",
    "check_pricing_inconsistency": "Medium",
}


def prepare_ledger(
    df_raw: pd.DataFrame,
    llm_mapping: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build working frame with canonical columns __L,__T,__U,__ID,__V,__D.

    When llm_mapping is provided (from model inference or remap_ledger), those
    column names are preferred; heuristics fill any missing role. All arithmetic
    on amounts is done here in pandas from the mapped columns.
    """
    out: dict[str, Any] = {
        "df": pd.DataFrame(),
        "columns": {},
        "original_columns": [],
        "error": None,
        "accumulated": {},
        "source_df": pd.DataFrame(),
        "mapping_source": "heuristic",
        "llm_rationale": None,
    }
    if df_raw is None or df_raw.empty:
        out["error"] = "empty_dataset"
        apply_post_prepare_enrichment(out)
        return out

    out["source_df"] = df_raw.copy()
    out["original_columns"] = list(df_raw.columns)

    lm: dict[str, str] = {}
    rationale = None
    if llm_mapping:
        rationale = llm_mapping.get("rationale")
        if isinstance(rationale, str):
            out["llm_rationale"] = rationale.strip() or None
        for k in MAP_KEYS:
            v = llm_mapping.get(k)
            if v is not None and str(v).strip():
                lm[k] = str(v).strip()
        if lm.get("amount_line"):
            out["mapping_source"] = "llm"

    df = df_raw.copy()
    for col in df.columns:
        if df[col].dtype == "object":
            df[col] = (
                df[col]
                .astype(str)
                .str.replace(r"[$,]", "", regex=True)
            )

    c_amt = _match_col(df, lm.get("amount_line")) or find_amt_col(
        df,
        ["BOOKING_VALUE_USD", "AMOUNT_USD", "Line_Amount", "Amount"],
    )
    c_tot = _match_col(df, lm.get("amount_total")) or find_col(
        df, ["NET_CASH_IMPACT_USD", "Invoice_Total", "Total"]
    )
    c_id = _match_col(df, lm.get("id")) or find_col(
        df, ["TRANSACTION_ID", "BOOKING_KEY", "Invoice_ID", "InvoiceID"]
    )
    c_unit = _match_col(df, lm.get("unit")) or find_col(
        df, ["FX_RATE_TO_USD", "Unit_Price", "Price"]
    )
    c_ven = _match_col(df, lm.get("vendor")) or find_col(
        df, ["SUPPLIER_KEY", "Vendor_Name", "Vendor"]
    )
    c_date = _match_col(df, lm.get("date")) or find_col(
        df, ["TRANSACTION_TS", "Invoice_Date", "Date"]
    )

    c_po = find_col(
        df,
        ["PO", "PurchaseOrder", "PONumber", "PO_Number", "Purchase_Order", "PO_Num"],
    )
    c_rcpt = find_col(
        df,
        ["Receipt", "Receiver", "GRN", "Goods_Receipt", "Receipt_Num", "Receipt_ID"],
    )
    c_tax = find_col(
        df, ["Tax_Amount", "Tax", "Sales_Tax", "VAT", "VAT_Amount", "TaxAmt"]
    )
    c_freight = find_col(
        df, ["Freight", "Shipping", "Ship_Cost", "Delivery_Fee", "Freight_Amt"]
    )
    c_ccy = find_col(
        df, ["Currency", "CUR", "CCY", "ISO_Currency", "Currency_Code"]
    )
    c_po_amt = find_col(
        df, ["PO_Amount", "Order_Amount", "Committed_Amt", "Open_PO_Value"]
    )

    cols_meta = {
        "amount_line": c_amt,
        "amount_total": c_tot,
        "id": c_id,
        "unit": c_unit,
        "vendor": c_ven,
        "date": c_date,
        "po": c_po,
        "receipt": c_rcpt,
        "tax_line": c_tax,
        "freight_line": c_freight,
        "currency": c_ccy,
        "po_amount": c_po_amt,
    }
    out["columns"] = cols_meta

    if not c_amt:
        out["error"] = "missing_amount_column"
        out["df"] = df
        apply_post_prepare_enrichment(out)
        return out

    if not c_id:
        df = df.copy()
        df["__ID"] = range(len(df))
        c_id = "__ID"

    df = df.copy()
    df["__L"] = pd.to_numeric(df[c_amt], errors="coerce").fillna(0)
    df["__T"] = (
        pd.to_numeric(df[c_tot], errors="coerce").fillna(0)
        if c_tot
        else df["__L"]
    )
    if c_unit:
        df["__U"] = pd.to_numeric(df[c_unit], errors="coerce").fillna(0)
    else:
        df["__U"] = 0.0
    df["__ID"] = df[c_id].astype(str)
    df["__V"] = df[c_ven].astype(str) if c_ven else "N/A"
    df["__D"] = (
        pd.to_datetime(df[c_date], errors="coerce")
        if c_date
        else pd.Series(pd.NaT, index=df.index)
    )

    z = pd.Series([""] * len(df), index=df.index, dtype=object)
    df["__PO"] = (
        df[c_po].astype(str).str.strip().replace({"nan": "", "None": ""})
        if c_po
        else z
    )
    df["__RCPT"] = (
        df[c_rcpt].astype(str).str.strip().replace({"nan": "", "None": ""})
        if c_rcpt
        else z.copy()
    )
    df["__TAX"] = (
        pd.to_numeric(df[c_tax], errors="coerce").fillna(0)
        if c_tax
        else pd.Series(0.0, index=df.index)
    )
    df["__FR"] = (
        pd.to_numeric(df[c_freight], errors="coerce").fillna(0)
        if c_freight
        else pd.Series(0.0, index=df.index)
    )
    df["__CCY"] = (
        df[c_ccy].astype(str).str.strip().str.upper().replace({"NAN": ""})
        if c_ccy
        else pd.Series([""] * len(df), index=df.index)
    )
    df["__PO_AMT"] = (
        pd.to_numeric(df[c_po_amt], errors="coerce").fillna(0)
        if c_po_amt
        else pd.Series(0.0, index=df.index)
    )

    out["df"] = df
    out["error"] = None
    apply_post_prepare_enrichment(out)
    return out


def tool_remap_ledger(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """Rebuild ledger from source_df using LLM-supplied column names; clears prior tool cache."""
    raw = ctx.get("source_df")
    if raw is None or (hasattr(raw, "empty") and raw.empty):
        return {"error": "no_source_df", "hint": "Upload a file first."}
    mapping = {}
    for k in MAP_KEYS:
        v = args.get(k)
        if v is not None and str(v).strip():
            mapping[k] = str(v).strip()
    if "amount_line" not in mapping:
        return {
            "error": "amount_line_required",
            "hint": "You must set amount_line to an exact column name from the file.",
        }
    fresh = prepare_ledger(raw.copy(), llm_mapping=mapping)
    ctx.clear()
    ctx.update(fresh)
    ctx["accumulated"] = {}
    return {
        "ok": not bool(fresh.get("error")),
        "columns": fresh.get("columns"),
        "mapping_source": fresh.get("mapping_source"),
        "llm_rationale": fresh.get("llm_rationale"),
        "row_count": len(fresh["df"]) if fresh.get("df") is not None else 0,
        "ledger_error": fresh.get("error"),
    }


def _records(df: pd.DataFrame, max_rows: int = 300) -> list[dict]:
    if df is None or df.empty:
        return []
    view = df.head(max_rows).copy()
    # Stable row identity so we can dedupe $ exposure across checks (same line, many flags).
    view.insert(0, "__ROW_IDX", view.index.to_numpy())
    for c in view.columns:
        if pd.api.types.is_datetime64_any_dtype(view[c]):
            view[c] = view[c].astype(str)
    records = view.replace({pd.NA: None}).to_dict(orient="records")
    # JSON-safe: replace NaN/inf
    safe: list[dict] = []
    for row in records:
        safe.append(
            {
                k: (
                    None
                    if v is not None and isinstance(v, float) and (math.isnan(v) or math.isinf(v))
                    else v
                )
                for k, v in row.items()
            }
        )
    return safe


def _vendor_breakdown(df: pd.DataFrame) -> list[dict]:
    if df is None or df.empty:
        return []
    g = df.groupby("__V", dropna=False)["__L"].sum().sort_values(ascending=False)
    return [{"vendor": str(v), "exposure_line_amount": float(g[v])} for v in g.index]


def tool_get_data_summary(ctx: dict[str, Any]) -> dict[str, Any]:
    err = ctx.get("error")
    if err:
        raw = ctx.get("source_df")
        if raw is not None and not raw.empty:
            return {
                "error": err,
                "hint": _hint_for_error(err),
                "row_count": int(len(raw)),
                "original_column_names": list(raw.columns),
                "note": "Call remap_ledger with exact column names from original_column_names, then re-run checks. "
                "Arithmetic runs in Python on the mapped columns.",
            }
        return {"error": err, "hint": _hint_for_error(err)}
    df = ctx["df"]
    dmin = df["__D"].min()
    dmax = df["__D"].max()
    return {
        "row_count": int(len(df)),
        "columns_detected": ctx["columns"],
        "mapping_source": ctx.get("mapping_source"),
        "original_column_names": ctx.get("original_columns", []),
        "distinct_vendors": int(df["__V"].nunique()),
        "date_range": {
            "min": str(dmin) if pd.notna(dmin) else None,
            "max": str(dmax) if pd.notna(dmax) else None,
        },
        "total_line_amount_usd": float(df["__L"].sum()),
        "has_unit_prices": bool(ctx["columns"].get("unit")),
        "llm_rationale": ctx.get("llm_rationale"),
        "note": "Totals and exposure estimates are computed in code from mapped columns — use them to prioritize vendor follow-up and recoveries.",
        "extended_checks_enabled": True,
    }


# UI / drilldown order (tool_key, section title)
DRILLDOWN_TOOL_ORDER: list[tuple[str, str]] = [
    ("check_math_integrity", "Math Integrity Check"),
    ("find_duplicate_invoices", "Duplicate Invoice"),
    ("find_duplicate_payment_patterns", "Vendor + date + amount pattern"),
    ("find_id_vendor_conflict", "ID / vendor conflict"),
    ("find_round_amount_signals", "Round amounts"),
    ("find_near_approval_threshold", "Near approval threshold"),
    ("find_weekend_postings", "Weekend posting dates"),
    ("find_high_velocity_vendor_days", "High-velocity vendor day"),
    ("find_near_duplicate_lines", "Near-duplicate lines"),
    ("find_tax_freight_anomalies", "Tax / freight vs line"),
    ("find_missing_po_receipt", "Missing PO / receipt"),
    ("find_line_over_po_open", "Line over PO amount"),
    ("find_stale_invoices", "Stale invoice dates"),
    ("find_data_quality_gaps", "Data quality gaps"),
    ("find_currency_mismatch", "Currency (non-dominant)"),
    ("find_benford_anomaly", "Benford-style check"),
    ("find_split_invoice_heuristic", "Possible split invoices"),
    ("find_intercompany_keywords", "Intercompany keywords"),
    ("detect_price_creep", "Price Creep"),
    ("find_negative_leaks", "Negative Leak"),
    ("check_pricing_inconsistency", "Pricing Inconsistency"),
]


def _hint_for_error(err: str) -> str:
    if err == "missing_amount_column":
        return "No line amount column found; include Amount, Line_Amount, AMOUNT_USD, or BOOKING_VALUE_USD."
    if err == "empty_dataset":
        return "File had no rows."
    return err


def tool_check_math_integrity(ctx: dict[str, Any]) -> dict[str, Any]:
    if ctx.get("error"):
        return {"error": ctx["error"]}
    df = ctx["df"]
    L = pd.to_numeric(df["__L"], errors="coerce")
    T = pd.to_numeric(df["__T"], errors="coerce")
    valid = L.notna() & T.notna()
    tol_c = int(round(float(os.environ.get("CLEARSPEND_CLASSIC_MATH_TOLERANCE_CENTS", "11"))))
    Lc = np.rint(np.asarray(L, dtype=np.float64) * 100.0)
    Tc = np.rint(np.asarray(T, dtype=np.float64) * 100.0)
    delta_c = np.abs(Tc - Lc)
    bad = valid.to_numpy() & (delta_c > tol_c)
    mm = df.loc[bad].copy()
    mm["delta_usd"] = np.round((Tc[bad] - Lc[bad]) / 100.0, 2)
    exposure = float(np.abs(Tc[bad] - Lc[bad]).sum() / 100.0) if bad.any() else 0.0
    return {
        "category": "Math Integrity Check",
        "priority": "Critical",
        "flagged_row_count": int(len(mm)),
        "exposure_usd": exposure,
        "by_vendor": _vendor_breakdown(mm),
        "flagged_rows": _records(mm),
    }


def tool_find_duplicate_invoices(ctx: dict[str, Any]) -> dict[str, Any]:
    if ctx.get("error"):
        return {"error": ctx["error"]}
    df = ctx["df"]
    dup_mask = df["__ID"].duplicated(keep=False)
    valid_id = df["__ID"].astype(str).str.upper() != "N/A"
    flagged = df[dup_mask & valid_id].copy()
    exposure = float(flagged["__L"].sum()) if not flagged.empty else 0.0
    return {
        "category": "Duplicate Invoice",
        "priority": "Critical",
        "flagged_row_count": int(len(flagged)),
        "exposure_usd": exposure,
        "duplicate_ids": flagged["__ID"].unique().tolist()[:500],
        "by_vendor": _vendor_breakdown(flagged),
        "flagged_rows": _records(flagged),
    }


def tool_find_duplicate_payment_patterns(ctx: dict[str, Any]) -> dict[str, Any]:
    """
    Same vendor + calendar day + same line amount (rounded) repeated — typical
    double-posting or duplicate receipt risk even when a transaction id differs.
    """
    if ctx.get("error"):
        return {"error": ctx["error"]}
    df = ctx["df"]
    sub = df[pd.notna(df["__D"])].copy()
    if sub.empty:
        return {
            "category": "Duplicate payment pattern (vendor + date + amount)",
            "priority": "High",
            "flagged_row_count": 0,
            "exposure_usd": 0.0,
            "note": "No parseable transaction dates; cannot evaluate date-anchored duplicate patterns.",
            "flagged_rows": [],
            "by_vendor": [],
        }
    sub["__day"] = pd.to_datetime(sub["__D"], errors="coerce").dt.normalize()
    sub = sub[pd.notna(sub["__day"])].copy()
    sub["__amt_r"] = sub["__L"].round(2)
    keys = ["__V", "__day", "__amt_r"]
    sub["_pat_n"] = sub.groupby(keys)["__L"].transform("count")
    flagged = sub[sub["_pat_n"] > 1].copy()
    exposure = 0.0
    for _k, g in flagged.groupby(keys):
        n = int(len(g))
        a = abs(float(g["__L"].iloc[0]))
        exposure += a * (n - 1)
    flagged["pattern_calendar_date"] = flagged["__day"].dt.strftime("%Y-%m-%d")
    flagged["pattern_amount_rounded"] = flagged["__amt_r"]
    show = flagged.drop(
        columns=["__day", "__amt_r", "_pat_n"],
        errors="ignore",
    )
    return {
        "category": "Duplicate payment pattern (vendor + date + amount)",
        "priority": "High",
        "flagged_row_count": int(len(flagged)),
        "exposure_usd": float(exposure),
        "note": "Flags rows sharing vendor, posting date, and amount — review before paying; exposure assumes (n−1) extra pays per cluster.",
        "by_vendor": _vendor_breakdown(flagged),
        "flagged_rows": _records(show),
    }


def tool_detect_price_creep(ctx: dict[str, Any]) -> dict[str, Any]:
    if ctx.get("error"):
        return {"error": ctx["error"]}
    df = ctx["df"].sort_values("__D")
    rows: list[dict] = []
    total = 0.0
    for v, group in df.groupby("__V"):
        if len(group) < 2:
            continue
        g = group.sort_values("__D")
        first_u = float(g["__U"].iloc[0])
        last_u = float(g["__U"].iloc[-1])
        diff = last_u - first_u
        if diff > 0:
            bump = diff * len(g)
            total += bump
            rows.append(
                {
                    "vendor": str(v),
                    "first_unit_price": first_u,
                    "last_unit_price": last_u,
                    "change_per_unit": round(diff, 6),
                    "rows_affected": int(len(g)),
                    "approx_exposure_usd": round(bump, 2),
                    "first_date": str(g["__D"].iloc[0]) if pd.notna(g["__D"].iloc[0]) else None,
                    "last_date": str(g["__D"].iloc[-1]) if pd.notna(g["__D"].iloc[-1]) else None,
                }
            )
    creep_df = pd.DataFrame(rows)
    return {
        "category": "Price Creep",
        "priority": "High",
        "flagged_row_count": int(sum(r["rows_affected"] for r in rows)),
        "exposure_usd": float(total),
        "by_vendor": [{"vendor": r["vendor"], "exposure_line_amount": r["approx_exposure_usd"]} for r in rows],
        "creep_detail": rows,
        "flagged_rows": rows[:300],
    }


def tool_find_negative_leaks(ctx: dict[str, Any]) -> dict[str, Any]:
    if ctx.get("error"):
        return {"error": ctx["error"]}
    df = ctx["df"]
    negs = df[df["__L"] < 0].copy()
    exposure = float(negs["__L"].abs().sum()) if not negs.empty else 0.0
    return {
        "category": "Negative Leak",
        "priority": "High",
        "flagged_row_count": int(len(negs)),
        "exposure_usd": exposure,
        "by_vendor": _vendor_breakdown(negs),
        "flagged_rows": _records(negs),
    }


def tool_check_pricing_inconsistency(ctx: dict[str, Any]) -> dict[str, Any]:
    if ctx.get("error"):
        return {"error": ctx["error"]}
    df = ctx["df"]
    c_unit = ctx["columns"].get("unit")
    if not c_unit:
        return {
            "category": "Pricing Inconsistency",
            "priority": "Medium",
            "flagged_row_count": 0,
            "exposure_usd": 0.0,
            "note": "No unit price column detected; skipped.",
            "flagged_rows": [],
            "by_vendor": [],
        }
    flagged_parts: list[pd.DataFrame] = []
    total_exposure = 0.0
    for _v, g in df.groupby("__V"):
        if g["__U"].nunique() <= 1:
            continue
        u_min = float(g["__U"].min())
        u_max = float(g["__U"].max())
        spread = u_max - u_min
        if spread <= 0:
            continue
        vendor_exp = spread * float(len(g))
        total_exposure += vendor_exp
        part = g.copy()
        part["__price_spread"] = round(spread, 6)
        flagged_parts.append(part)
    if not flagged_parts:
        return {
            "category": "Pricing Inconsistency",
            "priority": "Medium",
            "flagged_row_count": 0,
            "exposure_usd": 0.0,
            "flagged_rows": [],
            "by_vendor": [],
        }
    all_flagged = pd.concat(flagged_parts, ignore_index=True)
    return {
        "category": "Pricing Inconsistency",
        "priority": "Medium",
        "flagged_row_count": int(len(all_flagged)),
        "exposure_usd": float(total_exposure),
        "by_vendor": _vendor_breakdown(all_flagged),
        "flagged_rows": _records(all_flagged),
    }


def tool_find_id_vendor_conflict(ctx: dict[str, Any]) -> dict[str, Any]:
    """Same transaction/invoice id appearing under more than one vendor."""
    if ctx.get("error"):
        return {"error": ctx["error"]}
    df = ctx["df"]
    if df.empty:
        return {
            "category": "ID vendor conflict",
            "priority": "High",
            "flagged_row_count": 0,
            "exposure_usd": 0.0,
            "flagged_rows": [],
            "by_vendor": [],
        }
    if str(df["__ID"].iloc[0]).isdigit() and df["__ID"].astype(str).str.match(r"^\d+$").all():
        nids = df["__ID"].astype(int)
        if nids.min() == 0 and nids.max() == len(df) - 1:
            return {
                "category": "ID vendor conflict",
                "priority": "High",
                "flagged_row_count": 0,
                "exposure_usd": 0.0,
                "note": "Synthetic row ids detected; skipped.",
                "flagged_rows": [],
                "by_vendor": [],
            }
    g = df.groupby("__ID")["__V"].nunique()
    bad_ids = g[g > 1].index
    flagged = df[df["__ID"].isin(bad_ids)].copy()
    exposure = float(flagged["__L"].abs().sum()) if not flagged.empty else 0.0
    return {
        "category": "ID vendor conflict",
        "priority": "Critical",
        "flagged_row_count": int(len(flagged)),
        "exposure_usd": exposure,
        "conflict_ids": [str(x) for x in bad_ids.tolist()[:500]],
        "by_vendor": _vendor_breakdown(flagged),
        "flagged_rows": _records(flagged),
    }


def tool_find_round_amount_signals(ctx: dict[str, Any]) -> dict[str, Any]:
    """Large round / ‘clean’ amounts (may indicate manual entries or policy gaming)."""
    if ctx.get("error"):
        return {"error": ctx["error"]}
    df = ctx["df"]
    a = df["__L"].abs()
    exact_k = (a >= 5000) & (a % 1000 == 0)
    exact_h = (a >= 1000) & (a > 0) & (a % 100 == 0) & (a % 1000 != 0)
    flagged = df[exact_k | exact_h].copy()
    exposure = float(flagged["__L"].abs().sum()) if not flagged.empty else 0.0
    return {
        "category": "Round / clean amount signals",
        "priority": "Medium",
        "flagged_row_count": int(len(flagged)),
        "exposure_usd": exposure,
        "note": "Flags payments at exact thousands/hundreds thresholds — review context, not proof of fraud.",
        "by_vendor": _vendor_breakdown(flagged),
        "flagged_rows": _records(flagged),
    }


def tool_find_near_approval_threshold(ctx: dict[str, Any]) -> dict[str, Any]:
    """Amounts in upper bands just under common approval tiers (US-centric heuristic)."""
    if ctx.get("error"):
        return {"error": ctx["error"]}
    df = ctx["df"]
    a = df["__L"].abs()
    bands = [
        (4500, 5000),
        (9000, 10000),
        (24000, 25000),
        (49000, 50000),
    ]
    mask = pd.Series(False, index=df.index)
    for lo, hi in bands:
        mask = mask | ((a >= lo) & (a < hi))
    flagged = df[mask].copy()
    exposure = float(flagged["__L"].abs().sum()) if not flagged.empty else 0.0
    return {
        "category": "Near approval threshold",
        "priority": "Medium",
        "flagged_row_count": int(len(flagged)),
        "exposure_usd": exposure,
        "note": "Heuristic bands 5k/10k/25k/50k — tune for your policy limits.",
        "by_vendor": _vendor_breakdown(flagged),
        "flagged_rows": _records(flagged),
    }


def tool_find_weekend_postings(ctx: dict[str, Any]) -> dict[str, Any]:
    if ctx.get("error"):
        return {"error": ctx["error"]}
    df = ctx["df"]
    d = pd.to_datetime(df["__D"], errors="coerce")
    wd = d.dt.dayofweek
    flagged = df[(wd == 5) | (wd == 6)].copy()
    exposure = float(flagged["__L"].abs().sum()) if not flagged.empty else 0.0
    return {
        "category": "Weekend posting dates",
        "priority": "Low",
        "flagged_row_count": int(len(flagged)),
        "exposure_usd": exposure,
        "note": "Saturday/Sunday transaction dates — often batch timing; verify for duplicates.",
        "by_vendor": _vendor_breakdown(flagged),
        "flagged_rows": _records(flagged),
    }


def tool_find_high_velocity_vendor_days(ctx: dict[str, Any]) -> dict[str, Any]:
    if ctx.get("error"):
        return {"error": ctx["error"]}
    df = ctx["df"].copy()
    df["__day"] = pd.to_datetime(df["__D"], errors="coerce").dt.normalize()
    sub = df[pd.notna(df["__day"])]
    if sub.empty:
        return {
            "category": "High-velocity vendor day",
            "priority": "Medium",
            "flagged_row_count": 0,
            "exposure_usd": 0.0,
            "note": "No parseable dates.",
            "flagged_rows": [],
            "by_vendor": [],
        }
    vc = sub.groupby(["__V", "__day"], observed=True).size().reset_index(name="cnt")
    floor = max(8, int(vc["cnt"].quantile(0.90)))
    hot = vc[vc["cnt"] >= floor]
    flagged = sub.merge(
        hot[["__V", "__day"]],
        on=["__V", "__day"],
        how="inner",
    )
    exposure = float(flagged["__L"].abs().sum()) if not flagged.empty else 0.0
    return {
        "category": "High-velocity vendor day",
        "priority": "Medium",
        "flagged_row_count": int(len(flagged)),
        "exposure_usd": exposure,
        "note": f"Vendor-days with ≥{floor} lines (90th percentile or floor 8).",
        "by_vendor": _vendor_breakdown(flagged),
        "flagged_rows": _records(flagged.drop(columns=["__day"], errors="ignore")),
    }


def tool_find_near_duplicate_lines(ctx: dict[str, Any]) -> dict[str, Any]:
    """Same vendor + rounded amount within ~3 calendar days, different ids."""
    if ctx.get("error"):
        return {"error": ctx["error"]}
    df = ctx["df"].copy()
    df["__day"] = pd.to_datetime(df["__D"], errors="coerce").dt.normalize()
    df["__lr"] = df["__L"].round(2)
    flagged_idx: set[Any] = set()
    for (_, lr), g in df.groupby(["__V", "__lr"]):
        if lr == 0 or len(g) < 2:
            continue
        g2 = g[g["__day"].notna()]
        if g2.empty:
            continue
        span = (g2["__day"].max() - g2["__day"].min()).days
        if span <= 3 and g2["__ID"].nunique() > 1:
            flagged_idx.update(g2.index.tolist())
    if not flagged_idx:
        return {
            "category": "Near-duplicate lines (vendor+amount+window)",
            "priority": "High",
            "flagged_row_count": 0,
            "exposure_usd": 0.0,
            "flagged_rows": [],
            "by_vendor": [],
        }
    flagged = df.loc[list(flagged_idx)].sort_index().copy()
    exposure = float(flagged["__L"].abs().sum()) if not flagged.empty else 0.0
    show = flagged.drop(columns=["__day", "__lr"], errors="ignore")
    return {
        "category": "Near-duplicate lines (vendor+amount+window)",
        "priority": "High",
        "flagged_row_count": int(len(flagged)),
        "exposure_usd": exposure,
        "by_vendor": _vendor_breakdown(flagged),
        "flagged_rows": _records(show),
    }


def tool_find_tax_freight_anomalies(ctx: dict[str, Any]) -> dict[str, Any]:
    if ctx.get("error"):
        return {"error": ctx["error"]}
    df = ctx["df"]
    if (df["__TAX"].eq(0).all() and df["__FR"].eq(0).all()):
        return {
            "category": "Tax / freight vs line",
            "priority": "Medium",
            "flagged_row_count": 0,
            "exposure_usd": 0.0,
            "note": "No tax or freight columns detected.",
            "flagged_rows": [],
            "by_vendor": [],
        }
    line = df["__L"].abs().replace(0, pd.NA)
    rat_tax = (df["__TAX"].abs() / line).fillna(0)
    rat_fr = (df["__FR"].abs() / line).fillna(0)
    flagged = df[(rat_tax > 0.35) | (rat_fr > 0.35) | ((df["__TAX"] + df["__FR"]).abs() > df["__L"].abs())].copy()
    exposure = float(flagged["__L"].abs().sum()) if not flagged.empty else 0.0
    return {
        "category": "Tax / freight vs line",
        "priority": "Medium",
        "flagged_row_count": int(len(flagged)),
        "exposure_usd": exposure,
        "by_vendor": _vendor_breakdown(flagged),
        "flagged_rows": _records(flagged),
    }


def tool_find_missing_po_receipt(ctx: dict[str, Any]) -> dict[str, Any]:
    if ctx.get("error"):
        return {"error": ctx["error"]}
    df = ctx["df"]
    c = ctx.get("columns") or {}
    has_po = bool(c.get("po"))
    has_rcpt = bool(c.get("receipt"))
    if not has_po and not has_rcpt:
        return {
            "category": "Missing PO / receipt",
            "priority": "Medium",
            "flagged_row_count": 0,
            "exposure_usd": 0.0,
            "note": "No PO or receipt column detected.",
            "flagged_rows": [],
            "by_vendor": [],
        }
    high = df["__L"].abs() > 2500
    bad_po = (df["__PO"] == "") | (df["__PO"].str.upper() == "N/A") if has_po else pd.Series(True, index=df.index)
    bad_rc = (df["__RCPT"] == "") | (df["__RCPT"].str.upper() == "N/A") if has_rcpt else pd.Series(True, index=df.index)
    flagged = df[high & bad_po & bad_rc].copy()
    exposure = float(flagged["__L"].abs().sum()) if not flagged.empty else 0.0
    return {
        "category": "Missing PO / receipt",
        "priority": "High",
        "flagged_row_count": int(len(flagged)),
        "exposure_usd": exposure,
        "note": "Line |amount| > 2500 with blank PO and receipt (if columns exist).",
        "by_vendor": _vendor_breakdown(flagged),
        "flagged_rows": _records(flagged),
    }


def tool_find_line_over_po_open(ctx: dict[str, Any]) -> dict[str, Any]:
    if ctx.get("error"):
        return {"error": ctx["error"]}
    df = ctx["df"]
    if (ctx.get("columns") or {}).get("po_amount") is None:
        return {
            "category": "Line amount vs PO open",
            "priority": "Medium",
            "flagged_row_count": 0,
            "exposure_usd": 0.0,
            "note": "No PO amount / open value column detected.",
            "flagged_rows": [],
            "by_vendor": [],
        }
    flagged = df[(df["__PO_AMT"] > 0) & (df["__L"].abs() > df["__PO_AMT"] * 1.02)].copy()
    exposure = float((flagged["__L"].abs() - flagged["__PO_AMT"]).clip(lower=0).sum()) if not flagged.empty else 0.0
    return {
        "category": "Line amount vs PO open",
        "priority": "High",
        "flagged_row_count": int(len(flagged)),
        "exposure_usd": exposure,
        "note": "Line exceeds PO/open amount by >2%.",
        "by_vendor": _vendor_breakdown(flagged),
        "flagged_rows": _records(flagged),
    }


def tool_find_stale_invoices(ctx: dict[str, Any]) -> dict[str, Any]:
    if ctx.get("error"):
        return {"error": ctx["error"]}
    df = ctx["df"]
    d = pd.to_datetime(df["__D"], errors="coerce")
    ref = d.max()
    if pd.isna(ref):
        return {
            "category": "Stale invoice dates",
            "priority": "Low",
            "flagged_row_count": 0,
            "exposure_usd": 0.0,
            "note": "No valid dates.",
            "flagged_rows": [],
            "by_vendor": [],
        }
    cutoff = ref - pd.Timedelta(days=365)
    flagged = df[(d < cutoff) & (df["__L"].abs() > 0.01)].copy()
    exposure = float(flagged["__L"].abs().sum()) if not flagged.empty else 0.0
    return {
        "category": "Stale invoice dates",
        "priority": "Low",
        "flagged_row_count": int(len(flagged)),
        "exposure_usd": exposure,
        "note": "Invoice date >365 days before latest date in file.",
        "by_vendor": _vendor_breakdown(flagged),
        "flagged_rows": _records(flagged),
    }


def tool_find_data_quality_gaps(ctx: dict[str, Any]) -> dict[str, Any]:
    if ctx.get("error"):
        return {"error": ctx["error"]}
    df = ctx["df"]
    bad_ven = df["__V"].isin(["", "N/A", "nan", "NaN", "None"])
    bad_dt = df["__D"].isna()
    zero_line = df["__L"].abs() <= 0.01
    bad_tot = df["__T"].abs() > 0.05
    flagged = df[bad_ven | bad_dt | (zero_line & bad_tot)].copy()
    exposure = float(flagged["__T"].abs().sum() + flagged["__L"].abs().sum()) if not flagged.empty else 0.0
    return {
        "category": "Data quality gaps",
        "priority": "Medium",
        "flagged_row_count": int(len(flagged)),
        "exposure_usd": exposure,
        "note": "Missing vendor/date or zero line with non-zero total.",
        "by_vendor": _vendor_breakdown(flagged),
        "flagged_rows": _records(flagged),
    }


def tool_find_currency_mismatch(ctx: dict[str, Any]) -> dict[str, Any]:
    if ctx.get("error"):
        return {"error": ctx["error"]}
    df = ctx["df"]
    if (ctx.get("columns") or {}).get("currency") is None:
        return {
            "category": "Currency mix",
            "priority": "Low",
            "flagged_row_count": 0,
            "exposure_usd": 0.0,
            "note": "No currency column detected.",
            "flagged_rows": [],
            "by_vendor": [],
        }
    u = df.loc[df["__CCY"] != "", "__CCY"]
    if u.empty:
        return {
            "category": "Currency mix",
            "priority": "Low",
            "flagged_row_count": 0,
            "exposure_usd": 0.0,
            "note": "Currency column blank for all rows.",
            "flagged_rows": [],
            "by_vendor": [],
        }
    unique = u.unique()
    if len(unique) <= 1:
        return {
            "category": "Currency mix",
            "priority": "Low",
            "flagged_row_count": 0,
            "exposure_usd": 0.0,
            "note": "Single currency code in file.",
            "flagged_rows": [],
            "by_vendor": [],
        }
    mode = u.mode()
    dom = str(mode.iloc[0]) if not mode.empty else str(unique[0])
    flagged = df[df["__CCY"].ne("") & df["__CCY"].ne(dom)].copy()
    exposure = float(flagged["__L"].abs().sum()) if not flagged.empty else 0.0
    return {
        "category": "Currency mix",
        "priority": "Medium",
        "flagged_row_count": int(len(flagged)),
        "exposure_usd": exposure,
        "distinct_currencies": sorted({str(x) for x in unique.tolist()})[:30],
        "dominant_currency": dom,
        "note": "Rows where currency differs from the mode — review FX and consolidation.",
        "by_vendor": _vendor_breakdown(flagged),
        "flagged_rows": _records(flagged),
    }


def tool_find_benford_anomaly(ctx: dict[str, Any]) -> dict[str, Any]:
    if ctx.get("error"):
        return {"error": ctx["error"]}
    df = ctx["df"]
    pos = df[df["__L"] > 0]["__L"]
    if len(pos) < 100:
        return {
            "category": "Benford-style amount check",
            "priority": "Low",
            "flagged_row_count": 0,
            "exposure_usd": 0.0,
            "note": f"Need ≥100 positive lines for a coarse test (have {len(pos)}).",
            "flagged_rows": [],
            "by_vendor": [],
        }
    first = pos.astype(float).map(lambda x: int(str(abs(x)).split(".")[0][0]) if x >= 1 else 0)
    first = first[first > 0]
    if first.empty:
        return {
            "category": "Benford-style amount check",
            "priority": "Low",
            "flagged_row_count": 0,
            "exposure_usd": 0.0,
            "note": "No amounts ≥1 for leading digit.",
            "flagged_rows": [],
            "by_vendor": [],
        }
    p1 = float((first == 1).mean())
    alert = p1 < 0.22 or p1 > 0.34
    return {
        "category": "Benford-style amount check",
        "priority": "Low",
        "flagged_row_count": int(alert),
        "exposure_usd": float(pos.sum() * 0.01) if alert else 0.0,
        "share_first_digit_1": round(p1, 4),
        "note": "Coarse screen: share of amounts with leading digit 1 outside ~22–34% suggests manual review.",
        "flagged_rows": [],
        "by_vendor": [],
    }


def tool_find_split_invoice_heuristic(ctx: dict[str, Any]) -> dict[str, Any]:
    """Several lines same vendor+day that sum to a large round total — possible splits."""
    if ctx.get("error"):
        return {"error": ctx["error"]}
    df = ctx["df"].copy()
    df["__day"] = pd.to_datetime(df["__D"], errors="coerce").dt.normalize()
    parts: list[pd.DataFrame] = []
    exposure = 0.0
    for (v, day), g in df.groupby(["__V", "__day"]):
        if pd.isna(day) or len(g) < 3:
            continue
        s = float(g["__L"].sum())
        abs_s = abs(s)
        if abs_s >= 8000 and round(abs_s) % 1000 == 0:
            parts.append(g.copy())
            exposure += abs_s * 0.5
    if not parts:
        return {
            "category": "Possible split invoices",
            "priority": "Low",
            "flagged_row_count": 0,
            "exposure_usd": 0.0,
            "note": "No vendor-day clusters ≥3 lines summing to a large round total.",
            "flagged_rows": [],
            "by_vendor": [],
        }
    all_f = pd.concat(parts, ignore_index=True)
    return {
        "category": "Possible split invoices",
        "priority": "Low",
        "flagged_row_count": int(len(all_f)),
        "exposure_usd": float(exposure),
        "note": "Heuristic only: ≥3 lines same vendor+day summing to ≥8k near a round thousand.",
        "by_vendor": _vendor_breakdown(all_f),
        "flagged_rows": _records(all_f.drop(columns=["__day"], errors="ignore")),
    }


def tool_find_intercompany_keywords(ctx: dict[str, Any]) -> dict[str, Any]:
    if ctx.get("error"):
        return {"error": ctx["error"]}
    df = ctx["df"]
    ven = df["__V"].astype(str).str.upper()
    keys = [
        "INTERCOMPANY",
        "INTER-COMPANY",
        "INTERNAL",
        "IC SETTLEMENT",
        "IC CHARGE",
        "RELATED PARTY",
        "SUBSIDIARY",
    ]
    mask = pd.Series(False, index=df.index)
    for k in keys:
        mask = mask | ven.str.contains(re.escape(k), na=False)
    flagged = df[mask].copy()
    exposure = float(flagged["__L"].abs().sum()) if not flagged.empty else 0.0
    return {
        "category": "Intercompany / internal keywords",
        "priority": "Low",
        "flagged_row_count": int(len(flagged)),
        "exposure_usd": exposure,
        "note": "Vendor text matches intercompany-style keywords; validate allocation.",
        "by_vendor": _vendor_breakdown(flagged),
        "flagged_rows": _records(flagged),
    }


def tool_get_vendor_details(ctx: dict[str, Any], vendor_name: str) -> dict[str, Any]:
    if ctx.get("error"):
        return {"error": ctx["error"]}
    df = ctx["df"]
    name = (vendor_name or "").strip()
    if not name:
        return {"error": "vendor_name_required"}
    m = df["__V"].astype(str).str.fullmatch(re.escape(name), case=False)
    if not m.any():
        m = df["__V"].astype(str).str.contains(re.escape(name), case=False, regex=True)
    sub = df[m].copy()
    if sub.empty:
        return {"vendor_query": vendor_name, "match_count": 0, "rows": []}
    dup = sub["__ID"].duplicated(keep=False) & (
        sub["__ID"].astype(str).str.upper() != "N/A"
    )
    sL = pd.to_numeric(sub["__L"], errors="coerce")
    sT = pd.to_numeric(sub["__T"], errors="coerce")
    _tolc = int(round(float(os.environ.get("CLEARSPEND_CLASSIC_MATH_TOLERANCE_CENTS", "11"))))
    _Lc = np.rint(np.asarray(sL, dtype=np.float64) * 100.0)
    _Tc = np.rint(np.asarray(sT, dtype=np.float64) * 100.0)
    math_bad = pd.Series(np.abs(_Tc - _Lc) > _tolc, index=sub.index) & sL.notna() & sT.notna()
    neg = sub["__L"] < 0
    return {
        "vendor_query": vendor_name,
        "match_count": int(len(sub)),
        "line_amount_sum": float(sub["__L"].sum()),
        "duplicate_id_rows": int(dup.sum()),
        "math_mismatch_rows": int(math_bad.sum()),
        "negative_amount_rows": int(neg.sum()),
        "unit_price_unique_count": int(sub["__U"].nunique()),
        "ids": sub["__ID"].astype(str).unique().tolist()[:200],
        "rows": _records(sub),
    }


TOOL_DISPATCH = {
    "get_data_summary": lambda ctx, args: tool_get_data_summary(ctx),
    "remap_ledger": lambda ctx, args: tool_remap_ledger(ctx, args or {}),
    "check_math_integrity": lambda ctx, args: tool_check_math_integrity(ctx),
    "find_duplicate_invoices": lambda ctx, args: tool_find_duplicate_invoices(ctx),
    "find_duplicate_payment_patterns": lambda ctx, args: tool_find_duplicate_payment_patterns(
        ctx
    ),
    "find_id_vendor_conflict": lambda ctx, args: tool_find_id_vendor_conflict(ctx),
    "find_round_amount_signals": lambda ctx, args: tool_find_round_amount_signals(ctx),
    "find_near_approval_threshold": lambda ctx, args: tool_find_near_approval_threshold(
        ctx
    ),
    "find_weekend_postings": lambda ctx, args: tool_find_weekend_postings(ctx),
    "find_high_velocity_vendor_days": lambda ctx, args: tool_find_high_velocity_vendor_days(
        ctx
    ),
    "find_near_duplicate_lines": lambda ctx, args: tool_find_near_duplicate_lines(ctx),
    "find_tax_freight_anomalies": lambda ctx, args: tool_find_tax_freight_anomalies(ctx),
    "find_missing_po_receipt": lambda ctx, args: tool_find_missing_po_receipt(ctx),
    "find_line_over_po_open": lambda ctx, args: tool_find_line_over_po_open(ctx),
    "find_stale_invoices": lambda ctx, args: tool_find_stale_invoices(ctx),
    "find_data_quality_gaps": lambda ctx, args: tool_find_data_quality_gaps(ctx),
    "find_currency_mismatch": lambda ctx, args: tool_find_currency_mismatch(ctx),
    "find_benford_anomaly": lambda ctx, args: tool_find_benford_anomaly(ctx),
    "find_split_invoice_heuristic": lambda ctx, args: tool_find_split_invoice_heuristic(
        ctx
    ),
    "find_intercompany_keywords": lambda ctx, args: tool_find_intercompany_keywords(ctx),
    "detect_price_creep": lambda ctx, args: tool_detect_price_creep(ctx),
    "find_negative_leaks": lambda ctx, args: tool_find_negative_leaks(ctx),
    "check_pricing_inconsistency": lambda ctx, args: tool_check_pricing_inconsistency(
        ctx
    ),
    "get_vendor_details": lambda ctx, args: tool_get_vendor_details(
        ctx, str(args.get("vendor_name", ""))
    ),
}


def dispatch_tool(name: str, args: dict, ctx: dict[str, Any]) -> dict[str, Any]:
    fn = TOOL_DISPATCH.get(name)
    if not fn:
        return {"error": f"unknown_tool:{name}"}
    result = fn(ctx, args or {})
    result = apply_ignored_vendors_to_result(ctx, result)
    acc = ctx.setdefault("accumulated", {})
    acc[name] = result
    return result


MANDATORY_TOOLS = [
    "get_data_summary",
    "check_math_integrity",
    "find_duplicate_invoices",
    "find_duplicate_payment_patterns",
    "find_id_vendor_conflict",
    "find_round_amount_signals",
    "find_near_approval_threshold",
    "find_weekend_postings",
    "find_high_velocity_vendor_days",
    "find_near_duplicate_lines",
    "find_tax_freight_anomalies",
    "find_missing_po_receipt",
    "find_line_over_po_open",
    "find_stale_invoices",
    "find_data_quality_gaps",
    "find_currency_mismatch",
    "find_benford_anomaly",
    "find_split_invoice_heuristic",
    "find_intercompany_keywords",
    "detect_price_creep",
    "find_negative_leaks",
    "check_pricing_inconsistency",
]


def ensure_all_checks(ctx: dict[str, Any]) -> None:
    """Fill accumulated results so UI has full drilldown even if the agent skipped a tool."""
    for t in MANDATORY_TOOLS:
        if t not in ctx.get("accumulated", {}):
            dispatch_tool(t, {}, ctx)


def summary_table_from_accumulated(ctx: dict[str, Any]) -> pd.DataFrame:
    """Build the high-level Category / Amount table from accumulated tool results."""
    rows: list[dict] = []
    acc = ctx.get("accumulated", {})
    label = {
        "check_math_integrity": "Math Integrity Check",
        "find_duplicate_invoices": "Duplicate Invoice",
        "find_duplicate_payment_patterns": "Vendor + date + amount pattern",
        "find_id_vendor_conflict": "ID / vendor conflict",
        "find_round_amount_signals": "Round amounts",
        "find_near_approval_threshold": "Near approval threshold",
        "find_weekend_postings": "Weekend posting dates",
        "find_high_velocity_vendor_days": "High-velocity vendor day",
        "find_near_duplicate_lines": "Near-duplicate lines",
        "find_tax_freight_anomalies": "Tax / freight vs line",
        "find_missing_po_receipt": "Missing PO / receipt",
        "find_line_over_po_open": "Line over PO amount",
        "find_stale_invoices": "Stale invoice dates",
        "find_data_quality_gaps": "Data quality gaps",
        "find_currency_mismatch": "Currency (non-USD)",
        "find_benford_anomaly": "Benford-style check",
        "find_split_invoice_heuristic": "Possible split invoices",
        "find_intercompany_keywords": "Intercompany keywords",
        "detect_price_creep": "Price Creep",
        "find_negative_leaks": "Negative Leak",
        "check_pricing_inconsistency": "Pricing Inconsistency",
    }
    priority = {
        "check_math_integrity": "🔴 Critical",
        "find_duplicate_invoices": "🔴 Critical",
        "find_duplicate_payment_patterns": "🟠 High",
        "find_id_vendor_conflict": "🔴 Critical",
        "find_round_amount_signals": "🟡 Medium",
        "find_near_approval_threshold": "🟡 Medium",
        "find_weekend_postings": "⚪ Low",
        "find_high_velocity_vendor_days": "🟡 Medium",
        "find_near_duplicate_lines": "🟠 High",
        "find_tax_freight_anomalies": "🟡 Medium",
        "find_missing_po_receipt": "🟠 High",
        "find_line_over_po_open": "🟠 High",
        "find_stale_invoices": "⚪ Low",
        "find_data_quality_gaps": "🟡 Medium",
        "find_currency_mismatch": "🟡 Medium",
        "find_benford_anomaly": "⚪ Low",
        "find_split_invoice_heuristic": "⚪ Low",
        "find_intercompany_keywords": "⚪ Low",
        "detect_price_creep": "🟠 High",
        "find_negative_leaks": "🟣 High",
        "check_pricing_inconsistency": "🟡 Medium",
    }
    for key, title in label.items():
        r = acc.get(key) or {}
        if r.get("error"):
            continue
        exp = float(r.get("exposure_usd") or 0)
        n = int(r.get("flagged_row_count") or 0)
        if n == 0 and exp <= 0:
            continue
        rows.append(
            {
                "Category": title,
                "Amount ($)": exp,
                "Rows": n,
                "Priority": priority[key],
                "Confidence": TOOL_CONFIDENCE.get(key, "Medium"),
            }
        )
    return pd.DataFrame(rows)


def unique_flagged_line_exposure_from_accumulated(
    acc: dict[str, Any],
) -> tuple[float, int]:
    """
    Sum |__L| once per flagged row across all tools (see deduplicated_flagged_line_exposure).
    """
    by_key: dict[tuple[Any, ...], float] = {}
    for _tool, payload in acc.items():
        if not isinstance(payload, dict) or payload.get("error"):
            continue
        for row in payload.get("flagged_rows") or []:
            if not isinstance(row, dict):
                continue
            key_raw = row.get("__ROW_IDX")
            if key_raw is None and "__L" in row:
                key = (
                    "fp",
                    str(row.get("__ID", "")),
                    str(row.get("__V", "")),
                    round(float(row.get("__L") or 0.0), 6),
                    str(row.get("__D", "")),
                )
            elif key_raw is not None:
                key = ("idx", key_raw)
            else:
                continue
            try:
                amt = float(row.get("__L"))
            except (TypeError, ValueError):
                continue
            prev = by_key.get(key)
            # Same key should have same __L; keep max abs in case of tiny float noise
            v = abs(amt)
            if prev is None or v > prev:
                by_key[key] = v
    total = float(sum(by_key.values()))
    return total, len(by_key)


def deduplicated_flagged_line_exposure(ctx: dict[str, Any]) -> tuple[float, int]:
    """
    Dollar exposure counted once per source ledger row.

    The summary table sums per-category exposure_usd, which **double-counts** when the same
    line is flagged by multiple checks. This uses __ROW_IDX on flagged_rows (or a fallback
    fingerprint) to include each line's amount at most once.
    """
    ensure_all_checks(ctx)
    return unique_flagged_line_exposure_from_accumulated(ctx.get("accumulated") or {})


def format_accumulated_for_llm(ctx: dict[str, Any], max_chars: int = 6000) -> str:
    """Short text summary of tool outputs for chat context."""
    ensure_all_checks(ctx)
    parts: list[str] = []
    acc = ctx.get("accumulated", {})
    for name, payload in acc.items():
        if not isinstance(payload, dict) or payload.get("error"):
            continue
        exp = payload.get("exposure_usd")
        n = payload.get("flagged_row_count")
        if exp is not None:
            parts.append(
                f"- {name}: exposure_usd={exp}, flagged_rows={n}"
            )
        bv = payload.get("by_vendor") or []
        if bv and isinstance(bv, list):
            top = bv[:8]
            parts.append(
                "  top vendors: "
                + ", ".join(
                    f"{x.get('vendor')} (${x.get('exposure_line_amount')})"
                    for x in top
                    if isinstance(x, dict)
                )
            )
    text = "\n".join(parts)
    if len(text) > max_chars:
        return text[: max_chars - 20] + "\n...(truncated)"
    return text


def dataframe_from_tool_result(tool_name: str, payload: dict) -> pd.DataFrame | None:
    """Convert flagged_rows / creep_detail into a DataFrame for st.dataframe."""
    if not payload or payload.get("error"):
        return None
    if tool_name == "detect_price_creep" and payload.get("creep_detail"):
        return pd.DataFrame(payload["creep_detail"])
    if tool_name == "find_benford_anomaly" and payload.get("note"):
        return pd.DataFrame(
            [
                {
                    "share_first_digit_1": payload.get("share_first_digit_1"),
                    "detail": payload.get("note"),
                }
            ]
        )
    fr = payload.get("flagged_rows")
    if fr and isinstance(fr, list):
        return pd.DataFrame(fr)
    return None

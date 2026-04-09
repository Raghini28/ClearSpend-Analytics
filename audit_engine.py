"""Ledger normalization and forensic checks invoked by the agent and UI."""

from __future__ import annotations

import math
import re
from typing import Any

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

    cols_meta = {
        "amount_line": c_amt,
        "amount_total": c_tot,
        "id": c_id,
        "unit": c_unit,
        "vendor": c_ven,
        "date": c_date,
    }
    out["columns"] = cols_meta

    if not c_amt:
        out["error"] = "missing_amount_column"
        out["df"] = df
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

    out["df"] = df
    out["error"] = None
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
        "note": "Totals and audit metrics are computed in code from the mapped amount columns.",
    }


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
    mm = df[abs(df["__L"] - df["__T"]) > 0.05].copy()
    mm["delta_usd"] = (mm["__T"] - mm["__L"]).round(2)
    exposure = float(abs(mm["__T"] - mm["__L"]).sum()) if not mm.empty else 0.0
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
    math_bad = abs(sub["__L"] - sub["__T"]) > 0.05
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
    acc = ctx.setdefault("accumulated", {})
    acc[name] = result
    return result


MANDATORY_TOOLS = [
    "get_data_summary",
    "check_math_integrity",
    "find_duplicate_invoices",
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
        "detect_price_creep": "Price Creep",
        "find_negative_leaks": "Negative Leak",
        "check_pricing_inconsistency": "Pricing Inconsistency",
    }
    priority = {
        "check_math_integrity": "🔴 Critical",
        "find_duplicate_invoices": "🔴 Critical",
        "detect_price_creep": "🟠 High",
        "find_negative_leaks": "🟣 High",
        "check_pricing_inconsistency": "🟡 Medium",
    }
    for key, title in label.items():
        r = acc.get(key) or {}
        if r.get("error"):
            continue
        exp = float(r.get("exposure_usd") or 0)
        if exp <= 0 and int(r.get("flagged_row_count") or 0) == 0:
            continue
        rows.append(
            {
                "Category": title,
                "Amount ($)": exp,
                "Priority": priority[key],
            }
        )
    return pd.DataFrame(rows)


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
    fr = payload.get("flagged_rows")
    if fr and isinstance(fr, list):
        return pd.DataFrame(fr)
    return None

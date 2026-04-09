"""
Manufacturing AP ledger forensic workflow.

Distinguishes:
- Flagged exposure: dollars tied to suspicious / review rows (unique per invoice, no double count).
- Estimated savings: only exact duplicates, tax variance, and price-drift deltas (deduped per invoice).

Designed for CSV exports with ERP-style columns (see MANUFACTURING_LEDGER_COLUMNS).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

DATASET_GUIDE_MD = """### Dataset explanation (manufacturing AP)

**Purpose:** This dataset simulates a real-world accounts payable ledger for a manufacturing company over roughly two years. It is for vendor spend analytics, forensic AP auditing, duplicate payment detection, pricing leakage, and finance data quality controls.

**Design principles**
- The ledger is **mostly clean** and operationally realistic.
- Only a **small** subset of invoices contain intentional inconsistencies.
- Inconsistencies are **subtle** (not “everything is fraud”).
- **Target recoverable savings** on the reference file is on the order of **$12k–$16k** (rule defaults may shift slightly); **flagged exposure** is often larger and must be reported **separately** from savings.

**Core columns (use this framing in the app)**
| Column | What it means |
| ------ | -------------- |
| **Invoice_ID** | Unique invoice reference; may vary slightly for duplicates (e.g. INV-23391 vs INV-23391A). |
| **Vendor_Name** | Legal or trading name; minor spelling variants may exist (e.g. Gamma Supplies Ltd vs Gamma Supply Ltd). |
| **Vendor_ID** | Internal vendor master key; more stable than Vendor_Name. |
| **Category** | Spend category (Raw Materials, Logistics, MRO, Utilities, Capex, Packaging, IT Services, Facilities). |
| **Invoice_Date** | Date the vendor issued the invoice. |
| **Payment_Date** | Date the company paid. |
| **Currency** | Mostly USD; coding may show USD vs US$. |
| **Invoice_Amount** | Pre-tax amount. |
| **Tax** | Tax on the invoice. |
| **Total_Amount** | Usually Invoice_Amount + Tax. |
| **Payment_Status** | Usually Paid. |
| **Cost_Center** | Department / business unit code. |
| **Description** | Free-text description. |

**Intentional issues (what the audit should find)**
- **A. Confirmed duplicates** — same economic invoice with slight ID variation; **recoverable** if duplicate payments.
- **B. Near-duplicates** — same vendor, close dates, similar totals; **review only**, not automatic savings.
- **C. Overbilling / price drift** — small inflation vs peers; savings = **delta only**, not full invoice.
- **D. Tax inconsistencies** — savings = **tax delta**, not full invoice.
- **E. Data quality** — currency labels, vendor name variants, weekend dates, stale dates, near approval thresholds: **flag separately**, **not** savings.

**Three outputs (critical)**
1. **Estimated savings** — recoverable amounts from **exact duplicate extras**, **tax variance**, and **price drift** deltas only; **deduped per invoice** (max across those rules). **Not** math, near-dup, or control.
2. **Flagged exposure** — **review scope $**: max **Total_Amount** per line key for all **non-control** hits (math, substantive rules, near-dup pairs). **Not** cash recovery.
3. **Control issues** — weekend, currency coding, **payment lag**, thresholds, vendor name spellings: **row counts always**; **$ exposure** only for threshold / currency / lag (weekend & vendor spelling excluded from control $ total); **never** savings.

**Audit roles:** Python computes findings and KPIs; the **LLM only explains** from a compact brief (KPIs, top vendors, samples) — never the full multi-hundred-row file.
"""

MANUFACTURING_LEDGER_COLUMNS: tuple[str, ...] = (
    "Invoice_ID",
    "Vendor_Name",
    "Vendor_ID",
    "Category",
    "Invoice_Date",
    "Payment_Date",
    "Currency",
    "Invoice_Amount",
    "Tax",
    "Total_Amount",
    "Payment_Status",
    "Cost_Center",
    "Description",
)

SAVINGS_RULES = frozenset(
    {"Exact Duplicate", "Tax Variance", "Price Drift / Overbilling"}
)

FINDING_RECOVERABLE = "Recoverable"
FINDING_REVIEW = "Review Required"
FINDING_REFERENCE = "Reference"
FINDING_CONTROL = "Control Issue"

# Weekend / master-data spelling issues: keep rows for review but do not sum invoice $ into control exposure.
CONTROL_RULES_EXCLUDE_FROM_DOLLAR_EXPOSURE = frozenset(
    {"Weekend Posting Review", "Vendor Name Variation"}
)

# Control / hygiene — never estimated savings; optional row counts only.
CONTROL_FINDING_KEYS = frozenset(
    {"currency", "weekend", "threshold", "vendor_variation", "stale_invoice"}
)

# Substantive audit hits: used for “flagged exposure” = sum of ledger Total_Amount once per invoice.
SUBSTANTIVE_EXPOSURE_FINDING_KEYS = frozenset(
    {
        "math_integrity",
        "exact_duplicate",
        "tax_variance",
        "price_drift",
        "near_duplicate",
    }
)



def has_manufacturing_schema(df: pd.DataFrame) -> bool:
    """True if the frame has the expected ERP-style columns (case/spacing tolerant)."""
    norm = {_norm_col(c) for c in df.columns}
    for req in MANUFACTURING_LEDGER_COLUMNS:
        if _norm_col(req) not in norm:
            return False
    return True


def _norm_col(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _resolve_columns(df: pd.DataFrame) -> dict[str, str]:
    """Map canonical names to actual df column names."""
    mapping: dict[str, str] = {}
    lowered = { _norm_col(c): c for c in df.columns}
    for canon in MANUFACTURING_LEDGER_COLUMNS:
        key = _norm_col(canon)
        if key in lowered:
            mapping[canon] = lowered[key]
    return mapping


def normalize_ledger(df: pd.DataFrame, colmap: dict[str, str]) -> pd.DataFrame:
    """Single normalization step: strip text fields, parse dates, money rounded to cents."""
    out = df.rename(columns={v: k for k, v in colmap.items()}).copy()

    text_cols = [
        "Invoice_ID",
        "Vendor_Name",
        "Vendor_ID",
        "Category",
        "Currency",
        "Payment_Status",
        "Cost_Center",
        "Description",
    ]
    for col in text_cols:
        if col in out.columns:
            s = out[col]
            if pd.api.types.is_object_dtype(s):
                s = s.where(s.notna(), "").astype(str).str.strip()
            else:
                s = s.astype(str).str.strip()
            out[col] = s

    out["Vendor_Name_Clean"] = (
        out["Vendor_Name"]
        .str.lower()
        .str.replace(r"\s+", " ", regex=True)
    )
    out["Invoice_ID_Clean"] = out["Invoice_ID"].str.upper()
    out["Currency_Clean"] = (
        out["Currency"]
        .str.upper()
        .replace(
            {
                "US$": "USD",
                "US": "USD",
                "$": "USD",
            }
        )
    )

    out["Invoice_Date"] = pd.to_datetime(out["Invoice_Date"], errors="coerce")
    out["Payment_Date"] = pd.to_datetime(out["Payment_Date"], errors="coerce")

    for col in ("Invoice_Amount", "Tax", "Total_Amount"):
        s = out[col]
        if pd.api.types.is_object_dtype(s):
            s = s.astype(str).str.strip().str.replace(",", "", regex=False)
        out[col] = pd.to_numeric(s, errors="coerce").round(2)

    out["Vendor_ID"] = out["Vendor_ID"].astype(str).str.strip()
    out["Category"] = out["Category"].astype(str).str.strip()

    return out


def _tag_finding(
    df: pd.DataFrame,
    rule: str,
    severity: str,
    reason: str,
    *,
    recoverable_col: str | None = None,
    finding_type: str | None = None,
) -> pd.DataFrame:
    if df.empty:
        return df
    x = df.copy()
    x["Rule"] = rule
    x["Severity"] = severity
    x["Reason"] = reason
    if "Recoverable_Amount" not in x.columns:
        x["Recoverable_Amount"] = 0.0
    if recoverable_col and recoverable_col in x.columns:
        x["Recoverable_Amount"] = pd.to_numeric(
            x[recoverable_col], errors="coerce"
        ).fillna(0.0)
    x["Counts_Toward_Savings"] = rule in SAVINGS_RULES
    if finding_type is not None:
        x["Finding_Type"] = finding_type
    return x


def infer_embedded_fee_rate(norm: pd.DataFrame) -> float | None:
    """
    Median of (Total / (Invoice + Tax) − 1) when env ``CLEARSPEND_EMBEDDED_FEE_RATE`` is unset;
    used to avoid mass math flags when totals include a consistent % surcharge.
    """
    _inv = pd.to_numeric(norm["Invoice_Amount"], errors="coerce")
    _tax = pd.to_numeric(norm["Tax"], errors="coerce")
    _tot = pd.to_numeric(norm["Total_Amount"], errors="coerce")
    _valid = _inv.notna() & _tax.notna() & _tot.notna() & (_inv + _tax > 0)
    if int(_valid.sum()) <= 10:
        return None
    _implied = ((_tot[_valid] / (_inv[_valid] + _tax[_valid])) - 1.0).median()
    if abs(float(_implied)) > 0.001:
        return round(float(_implied), 6)
    return None


def math_integrity_check(
    norm: pd.DataFrame,
    tolerance: float | None = None,
    *,
    embedded_fee_rate: float | None = None,
) -> pd.DataFrame:
    """
    Total_Amount should equal (Invoice_Amount + Tax) × (1 + fee_rate) after cent rounding.

    Uses **integer cents** so float noise does not false-flag. Optional **embedded fee** when
    the ERP bakes a surcharge into Total (e.g. processing fee as % of pre-fee subtotal).

    If ``embedded_fee_rate`` is set (e.g. from :func:`infer_embedded_fee_rate`), it overrides
    env for this call only. Otherwise ``CLEARSPEND_EMBEDDED_FEE_RATE`` is used when set.

    Env:
    - CLEARSPEND_MATH_TOLERANCE — max abs delta in **dollars** (after cent comparison logic).
    - CLEARSPEND_MATH_EXTRA_PENNY — if not "0", widen tolerance by 1¢ when defaulting.
    - CLEARSPEND_EMBEDDED_FEE_RATE — decimal rate applied to (Invoice+Tax), e.g. ``0.009363``.
    """
    default_tol = 0.10
    extra_penny = os.environ.get("CLEARSPEND_MATH_EXTRA_PENNY", "0").strip() != "0"
    if tolerance is None:
        tolerance = default_tol
    env_tol = os.environ.get("CLEARSPEND_MATH_TOLERANCE")
    if env_tol is not None:
        try:
            tolerance = float(env_tol)
        except (TypeError, ValueError):
            tolerance = default_tol
    elif extra_penny and tolerance <= 0.10:
        tolerance = 0.11

    fee_rate = 0.0
    if embedded_fee_rate is not None:
        fee_rate = float(embedded_fee_rate)
    else:
        env_fee = os.environ.get("CLEARSPEND_EMBEDDED_FEE_RATE")
        if env_fee is not None:
            try:
                fee_rate = float(env_fee)
            except (TypeError, ValueError):
                fee_rate = 0.0

    tol_cents = max(1, int(round(float(tolerance) * 100)))

    out = norm.copy()
    inv = pd.to_numeric(out["Invoice_Amount"], errors="coerce")
    tax = pd.to_numeric(out["Tax"], errors="coerce")
    tot = pd.to_numeric(out["Total_Amount"], errors="coerce")

    valid = inv.notna() & tax.notna() & tot.notna()
    valid_arr = valid.to_numpy()

    v_inv = np.asarray(inv, dtype=np.float64)
    v_tax = np.asarray(tax, dtype=np.float64)
    v_tot = np.asarray(tot, dtype=np.float64)
    v_expected = (v_inv + v_tax) * (1.0 + fee_rate)
    exp_c = np.rint(v_expected * 100.0)
    tot_c = np.rint(v_tot * 100.0)
    delta_c = tot_c - exp_c

    out["Expected_Total"] = np.round(v_expected, 2)
    math_delta_vals = np.where(valid_arr, np.round(delta_c / 100.0, 2), np.nan)
    out["Math_Delta"] = pd.Series(math_delta_vals, index=out.index, dtype=float)

    abs_c = np.abs(delta_c)
    with np.errstate(invalid="ignore"):
        bad = valid_arr & (abs_c > tol_cents)

    flagged = out.loc[bad].copy()
    if flagged.empty:
        return flagged
    flagged["Recoverable_Amount"] = np.round(np.abs(delta_c[bad]) / 100.0, 2)
    reason = (
        "Total amount does not match (Invoice + Tax) × (1 + embedded fee) beyond tolerance "
        "(cent-rounded check)."
        if fee_rate != 0.0
        else "Total amount does not match invoice amount plus tax beyond tolerance "
        "(cent-rounded check)."
    )
    tagged = _tag_finding(
        flagged,
        "Math Integrity Check",
        "Medium",
        reason,
        recoverable_col="Recoverable_Amount",
        finding_type=FINDING_REVIEW,
    )
    return tagged


def exact_duplicate_check(norm: pd.DataFrame) -> pd.DataFrame:
    key_cols = [
        "Vendor_ID",
        "Invoice_Date",
        "Invoice_Amount",
        "Tax",
        "Total_Amount",
    ]
    dupes = norm[norm.duplicated(subset=key_cols, keep=False)].copy()
    if dupes.empty:
        return dupes
    dupes["Duplicate_Group_Size"] = dupes.groupby(key_cols)["Invoice_ID"].transform("count")
    # Deterministic “first” row = lowest Invoice_ID in group; only rank > 1 is recover duplicate pay.
    dupes = dupes.sort_values(key_cols + ["Invoice_ID"])
    dupes["Rank_In_Group"] = dupes.groupby(key_cols, sort=False).cumcount() + 1
    dupes["Recoverable_Amount"] = np.where(
        dupes["Rank_In_Group"] > 1,
        dupes["Total_Amount"],
        0.0,
    )
    tagged = _tag_finding(
        dupes,
        "Exact Duplicate",
        "High",
        "Duplicate invoice group detected; only extra copies beyond first are recoverable.",
        recoverable_col="Recoverable_Amount",
    )
    tagged["Finding_Type"] = np.where(
        tagged["Rank_In_Group"] > 1,
        FINDING_RECOVERABLE,
        FINDING_REFERENCE,
    )
    tagged["Counts_Toward_Savings"] = tagged["Finding_Type"] == FINDING_RECOVERABLE
    return tagged


def near_duplicate_check(
    norm: pd.DataFrame,
    date_window_days: int = 7,
    amount_tolerance: float = 5.0,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    work = norm.sort_values(["Vendor_ID", "Invoice_Date"]).reset_index(drop=True)

    for vendor_id, group in work.groupby("Vendor_ID", sort=False):
        g = group.sort_values("Invoice_Date").reset_index(drop=True)
        n = len(g)
        for i in range(n):
            for j in range(i + 1, n):
                d1 = g.loc[i, "Invoice_Date"]
                d2 = g.loc[j, "Invoice_Date"]
                if pd.isna(d1) or pd.isna(d2):
                    continue
                day_diff = abs((d2 - d1).days)
                if day_diff > date_window_days:
                    break
                amt_diff = abs(
                    float(g.loc[j, "Total_Amount"] or 0)
                    - float(g.loc[i, "Total_Amount"] or 0)
                )
                if amt_diff <= amount_tolerance:
                    mx = max(float(g.loc[i, "Total_Amount"] or 0), float(g.loc[j, "Total_Amount"] or 0))
                    rows.append(
                        {
                            "Invoice_ID_1": g.loc[i, "Invoice_ID"],
                            "Invoice_ID_2": g.loc[j, "Invoice_ID"],
                            "Vendor_ID": vendor_id,
                            "Vendor_Name": g.loc[i, "Vendor_Name"],
                            "Invoice_Date_1": d1,
                            "Invoice_Date_2": d2,
                            "Total_Amount_1": g.loc[i, "Total_Amount"],
                            "Total_Amount_2": g.loc[j, "Total_Amount"],
                            "Total_Amount": mx,
                            "Potential_Exposure": mx,
                            "Recoverable_Amount": 0.0,
                        }
                    )

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return _tag_finding(
        df,
        "Near Duplicate Review",
        "Medium",
        "Similar amount and date window within same vendor — review only, not savings.",
        finding_type=FINDING_REVIEW,
    )


def tax_variance_check(
    norm: pd.DataFrame,
    expected_rate: float | None = None,
    tolerance: float = 1.0,
) -> pd.DataFrame:
    """
    Flags rows where Tax deviates from **Expected_Tax = Invoice_Amount × rate** beyond tolerance.

    If ``expected_rate`` is not passed and ``CLEARSPEND_EXPECTED_TAX_RATE`` is unset, **rate** is
    the dataset's **median implied** ``Tax / Invoice_Amount`` (positive invoices only). That avoids
    false positives when the ledger consistently uses e.g. 8.07% vs a hardcoded 8%.
    """
    inv = pd.to_numeric(norm["Invoice_Amount"], errors="coerce")
    tx = pd.to_numeric(norm["Tax"], errors="coerce")
    valid_mask = inv.notna() & tx.notna() & (inv > 0)

    rate: float | None = None
    if expected_rate is not None:
        rate = float(expected_rate)
    else:
        env_r = os.environ.get("CLEARSPEND_EXPECTED_TAX_RATE")
        if env_r is not None:
            try:
                rate = float(env_r)
            except (TypeError, ValueError):
                rate = None
    if rate is None:
        implied = tx[valid_mask] / inv[valid_mask]
        rate = float(implied.median()) if not implied.empty else 0.08

    out = norm.copy()
    inv = pd.to_numeric(out["Invoice_Amount"], errors="coerce")
    tx = pd.to_numeric(out["Tax"], errors="coerce")
    out["Expected_Tax"] = (inv * float(rate)).round(2)
    out["Tax_Delta"] = (tx - out["Expected_Tax"]).round(2)
    valid = inv.notna() & tx.notna() & (inv > 0)
    flagged = out[valid & (out["Tax_Delta"].abs() > tolerance)].copy()
    if flagged.empty:
        return flagged
    flagged["Recoverable_Amount"] = flagged["Tax_Delta"].abs()
    return _tag_finding(
        flagged,
        "Tax Variance",
        "Low",
        f"Tax amount deviates from benchmark rate ({rate:.4%}) beyond ${tolerance:.2f} tolerance.",
        recoverable_col="Recoverable_Amount",
        finding_type=FINDING_RECOVERABLE,
    )


def price_drift_check(
    norm: pd.DataFrame,
    z_threshold: float = 3.0,
    min_group_size: int | None = None,
) -> pd.DataFrame:
    """
    Peer benchmark by Vendor_ID + Category (group median / std on **Invoice_Amount**).

    Recoverable_Amount = max(Invoice_Amount − Peer_Median, 0) only — **never** full invoice.
    Defaults: **z_threshold = 3.0**, **min_group_size = 30** (env ``CLEARSPEND_PRICE_DRIFT_MIN_GROUP``).
    """
    if min_group_size is None:
        try:
            min_group_size = int(os.environ.get("CLEARSPEND_PRICE_DRIFT_MIN_GROUP", "30"))
        except ValueError:
            min_group_size = 30
    out = norm.copy()
    grp = out.groupby(["Vendor_ID", "Category"], sort=False)["Invoice_Amount"]
    out["Peer_Count"] = grp.transform("count")
    out["Peer_Median"] = grp.transform("median")
    out["Peer_STD"] = grp.transform("std").fillna(0.0)

    eligible = out["Peer_Count"] >= int(min_group_size)
    threshold = out["Peer_Median"] + (float(z_threshold) * out["Peer_STD"])
    inv_amt = pd.to_numeric(out["Invoice_Amount"], errors="coerce")

    flagged = out[
        eligible
        & (out["Peer_STD"] > 0)
        & inv_amt.notna()
        & (inv_amt > threshold)
    ].copy()

    if flagged.empty:
        return flagged

    flagged["Price_Delta"] = (inv_amt.loc[flagged.index] - flagged["Peer_Median"]).round(2)
    flagged["Recoverable_Amount"] = (
        (inv_amt.loc[flagged.index] - flagged["Peer_Median"]).clip(lower=0).round(2)
    )
    return _tag_finding(
        flagged,
        "Price Drift / Overbilling",
        "Medium",
        "Estimated sourcing opportunity: pre-tax amount above peer median for same vendor and category.",
        recoverable_col="Recoverable_Amount",
        finding_type=FINDING_RECOVERABLE,
    )


def currency_consistency_check(norm: pd.DataFrame) -> pd.DataFrame:
    raw = norm["Currency"].astype(str).str.strip().str.upper()
    flagged = norm[raw.isin(["US$", "US", "$"])].copy()
    if flagged.empty:
        return flagged
    flagged["Recoverable_Amount"] = 0.0
    flagged["Total_Amount"] = flagged["Total_Amount"].fillna(0)
    return _tag_finding(
        flagged,
        "Currency Coding Inconsistency",
        "Low",
        "Non-standard currency code formatting — control issue, not savings.",
        finding_type=FINDING_CONTROL,
    )


def weekend_posting_check(norm: pd.DataFrame, *, use_payment_date: bool = False) -> pd.DataFrame:
    col = "Payment_Date" if use_payment_date else "Invoice_Date"
    out = norm.copy()
    out["_wd"] = out[col].dt.dayofweek
    flagged = out[out["_wd"] >= 5].drop(columns=["_wd"], errors="ignore")
    if flagged.empty:
        return flagged
    flagged = flagged.drop(columns=["_wd"], errors="ignore")
    flagged["Recoverable_Amount"] = 0.0
    return _tag_finding(
        flagged,
        "Weekend Posting Review",
        "Low",
        "Invoice date falls on weekend — control issue, not savings.",
        finding_type=FINDING_CONTROL,
    )


def threshold_check(
    norm: pd.DataFrame,
    thresholds: tuple[int, ...] = (5000, 10000, 25000, 50000),
    window: float = 250.0,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for _, row in norm.iterrows():
        amt = float(row["Total_Amount"] or 0)
        for t in thresholds:
            if abs(amt - t) <= window:
                x = row.to_dict()
                x["Threshold_Near"] = t
                x["Recoverable_Amount"] = 0.0
                rows.append(x)
                break
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return _tag_finding(
        df,
        "Near Approval Threshold",
        "Low",
        "Invoice total is close to approval threshold — control issue, not savings.",
        finding_type=FINDING_CONTROL,
    )


def stale_invoice_check(
    norm: pd.DataFrame,
    reference_date: pd.Timestamp | None = None,
    days_threshold: int | None = None,
) -> pd.DataFrame:
    """Flag **paid** rows where payment lag (Payment_Date − Invoice_Date) exceeds threshold.

    Calendar age vs ``max(Payment_Date)`` would flag most rows on multi-year extracts even when
    paid promptly; lag matches the operational AP control (slow posting / approval).
    ``reference_date`` is reserved for future unpaid/open-invoice logic and is unused here.
    """
    _ = reference_date
    days = 120 if days_threshold is None else int(days_threshold)
    env_d = os.environ.get("CLEARSPEND_STALE_INVOICE_DAYS")
    if env_d is not None and days_threshold is None:
        try:
            days = int(env_d)
        except ValueError:
            pass
    out = norm.copy()
    idt = pd.to_datetime(out["Invoice_Date"], errors="coerce")
    pdt = pd.to_datetime(out["Payment_Date"], errors="coerce")
    out["Payment_Lag_Days"] = (pdt - idt).dt.days
    flagged = out.loc[
        idt.notna() & pdt.notna() & (out["Payment_Lag_Days"] > days)
    ].copy()
    if flagged.empty:
        return flagged
    flagged["Recoverable_Amount"] = 0.0
    return _tag_finding(
        flagged,
        "Stale Invoice Review",
        "Low",
        f"Payment lag exceeds {days} days (payment date minus invoice date) — control issue, not savings.",
        finding_type=FINDING_CONTROL,
    )


def vendor_name_variation_signal(norm: pd.DataFrame) -> pd.DataFrame:
    """Rows where cleaned vendor name maps to multiple raw names for same Vendor_ID."""
    g = norm.groupby("Vendor_ID")["Vendor_Name_Clean"].nunique()
    multi = g[g > 1].index
    if len(multi) == 0:
        return pd.DataFrame()
    flagged = norm[norm["Vendor_ID"].isin(multi)].copy()
    flagged["Recoverable_Amount"] = 0.0
    return _tag_finding(
        flagged,
        "Vendor Name Variation",
        "Low",
        "Multiple vendor name spellings under one Vendor_ID — control issue, not savings.",
        finding_type=FINDING_CONTROL,
    )


@dataclass
class ManufacturingAuditResult:
    normalized: pd.DataFrame
    findings: dict[str, pd.DataFrame]
    flagged_exposure_usd: float
    estimated_savings_usd: float
    kpis: dict[str, Any]


def collect_substantive_flagged_invoice_ids(findings: dict[str, pd.DataFrame]) -> set[str]:
    """Invoices involved in math / dup / tax / drift / near-duplicate (excludes control-only)."""
    ids: set[str] = set()
    for key, fdf in findings.items():
        if key not in SUBSTANTIVE_EXPOSURE_FINDING_KEYS or fdf is None or fdf.empty:
            continue
        if "Invoice_ID" in fdf.columns:
            ids.update(fdf["Invoice_ID"].astype(str).str.strip().tolist())
        elif "Invoice_ID_1" in fdf.columns:
            ids.update(fdf["Invoice_ID_1"].astype(str).str.strip().tolist())
            ids.update(fdf["Invoice_ID_2"].astype(str).str.strip().tolist())
    return ids


def accumulate_unique_invoice_exposure_usd(
    findings: dict[str, pd.DataFrame], norm: pd.DataFrame
) -> dict[str, float]:
    """
    Per substantive-flagged invoice: ledger **Total_Amount** once (review exposure).

    Control signals (currency, weekend, stale, etc.) do not add invoices here.
    """
    ids = collect_substantive_flagged_invoice_ids(findings)
    amounts: dict[str, float] = {}
    if not ids or norm.empty:
        return amounts
    meta = norm.copy()
    meta["_iid"] = meta["Invoice_ID"].astype(str).str.strip()
    meta = meta.drop_duplicates(subset="_iid", keep="last").set_index("_iid")
    for iid in ids:
        if iid not in meta.index:
            amounts[iid] = 0.0
            continue
        row = meta.loc[iid]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        amounts[iid] = float(pd.to_numeric(row.get("Total_Amount"), errors="coerce") or 0.0)
    return amounts


def per_invoice_flagged_exposure(
    findings: dict[str, pd.DataFrame], norm: pd.DataFrame
) -> pd.DataFrame:
    """One row per substantive-flagged invoice: **Total_Amount** (review exposure) + vendor."""
    amounts = accumulate_unique_invoice_exposure_usd(findings, norm)

    if not amounts:
        return pd.DataFrame(
            columns=["Invoice_ID", "Vendor_ID", "Vendor_Name", "Exposure_USD"]
        )

    _meta_df = norm.copy()
    _meta_df["_iid"] = _meta_df["Invoice_ID"].astype(str)
    meta = _meta_df.drop_duplicates(subset="_iid", keep="last").set_index("_iid")
    rows = []
    for inv, exp in amounts.items():
        if inv not in meta.index:
            rows.append(
                {
                    "Invoice_ID": inv,
                    "Vendor_ID": "",
                    "Vendor_Name": "",
                    "Exposure_USD": exp,
                }
            )
            continue
        row = meta.loc[inv]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        rows.append(
            {
                "Invoice_ID": inv,
                "Vendor_ID": str(row.get("Vendor_ID", "") or ""),
                "Vendor_Name": str(row.get("Vendor_Name", "") or ""),
                "Exposure_USD": float(exp),
            }
        )
    return pd.DataFrame(rows)


def top_flagged_vendors_by_exposure(
    result: ManufacturingAuditResult, top_n: int = 10
) -> pd.DataFrame:
    """Rank vendors by sum of unique per-invoice flagged exposure."""
    inv_tbl = per_invoice_flagged_exposure(result.findings, result.normalized)
    if inv_tbl.empty:
        return pd.DataFrame(
            columns=["Vendor_ID", "Vendor_Name", "Flagged_Exposure_USD", "Invoice_Count"]
        )
    g = inv_tbl.groupby(["Vendor_ID", "Vendor_Name"], dropna=False).agg(
        Flagged_Exposure_USD=("Exposure_USD", "sum"),
        Invoice_Count=("Invoice_ID", "count"),
    )
    g = g.reset_index().sort_values("Flagged_Exposure_USD", ascending=False)
    return g.head(int(top_n)).reset_index(drop=True)


def compute_unique_flagged_exposure(
    findings: dict[str, pd.DataFrame], norm: pd.DataFrame
) -> float:
    """
    **Flagged exposure (review):** sum of **ledger Total_Amount** for each unique invoice
    that hits any substantive rule (math, exact dup, tax, price drift, near-dup).
    Control-only rows are excluded. Each invoice counted once.
    """
    return float(sum(accumulate_unique_invoice_exposure_usd(findings, norm).values()))


def compute_estimated_savings_deduped(findings: dict[str, pd.DataFrame]) -> float:
    """Conservative: per Invoice_ID max recoverable across savings-eligible rules only."""
    parts: list[pd.DataFrame] = []
    for name, fdf in findings.items():
        if fdf is None or fdf.empty:
            continue
        if "Counts_Toward_Savings" not in fdf.columns:
            continue
        sub = fdf[fdf["Counts_Toward_Savings"]].copy()
        if sub.empty:
            continue
        if "Invoice_ID" not in sub.columns:
            continue
        parts.append(sub[["Invoice_ID", "Recoverable_Amount"]])
    if not parts:
        return 0.0
    all_s = pd.concat(parts, ignore_index=True)
    all_s["Recoverable_Amount"] = pd.to_numeric(
        all_s["Recoverable_Amount"], errors="coerce"
    ).fillna(0)
    all_s = all_s.assign(_inv=all_s["Invoice_ID"].astype(str))
    by_inv = all_s.groupby("_inv", as_index=False)["Recoverable_Amount"].max()
    return float(by_inv["Recoverable_Amount"].sum())


def consolidate_findings(
    findings: dict[str, pd.DataFrame],
    *,
    norm: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """
    Single rollup: separates estimated savings, flagged (review) exposure, and control $ exposure.

    - **Estimated savings:** Recoverable + savings rules only; **max recoverable per Line_Key**
      (no double count across duplicate / tax / drift).
    - **Flagged exposure:** sum of **ledger Total_Amount once per invoice** that hits any substantive
      rule (math, dup, tax, drift, near-dup). Near-duplicate rows use pair keys internally, so this
      **must not** use raw ``Line_Key`` max sums (that would count the same invoice once per pair).
    - **Control issue exposure:** max exposure per line for control rules only.
    """
    frames: list[pd.DataFrame] = []
    for _rule_key, fdf in findings.items():
        if fdf is None or fdf.empty:
            continue
        temp = fdf.copy()
        if "Finding_Type" not in temp.columns:
            rname = str(temp["Rule"].iloc[0]) if "Rule" in temp.columns and len(temp) else ""
            if rname in SAVINGS_RULES:
                temp["Finding_Type"] = FINDING_RECOVERABLE
            else:
                temp["Finding_Type"] = FINDING_REVIEW
        if "Invoice_ID" in temp.columns:
            temp["Line_Key"] = temp["Invoice_ID"].astype(str).str.strip()
            temp["Exposure_Amount"] = (
                pd.to_numeric(temp.get("Total_Amount"), errors="coerce").fillna(0.0).abs()
            )
        else:
            i1 = temp["Invoice_ID_1"].astype(str).str.strip()
            i2 = temp["Invoice_ID_2"].astype(str).str.strip()
            pair_min = np.minimum(i1, i2)
            pair_max = np.maximum(i1, i2)
            temp["Line_Key"] = pair_min + "|" + pair_max
            temp["Exposure_Amount"] = pd.to_numeric(
                temp.get("Potential_Exposure", temp.get("Total_Amount")),
                errors="coerce",
            ).fillna(0.0).abs()
        if "Recoverable_Amount" not in temp.columns:
            temp["Recoverable_Amount"] = 0.0
        if "Rule" in temp.columns and "Finding_Type" in temp.columns:
            zd = temp["Rule"].isin(CONTROL_RULES_EXCLUDE_FROM_DOLLAR_EXPOSURE) & (
                temp["Finding_Type"] == FINDING_CONTROL
            )
            temp.loc[zd, "Exposure_Amount"] = 0.0
        frames.append(temp)

    if not frames:
        return {
            "all_findings": pd.DataFrame(),
            "estimated_savings": 0.0,
            "flagged_exposure": 0.0,
            "control_issue_exposure": 0.0,
            "recoverable_summary": pd.DataFrame(),
            "rule_summary": pd.DataFrame(),
        }

    all_findings = pd.concat(frames, ignore_index=True)
    all_findings["Recoverable_Amount"] = pd.to_numeric(
        all_findings["Recoverable_Amount"], errors="coerce"
    ).fillna(0.0)

    sav_mask = (all_findings["Finding_Type"] == FINDING_RECOVERABLE) & (
        all_findings["Rule"].isin(SAVINGS_RULES)
    )
    estimated_savings = float(
        all_findings.loc[sav_mask]
        .groupby("Line_Key", dropna=False)["Recoverable_Amount"]
        .max()
        .sum()
    )

    non_ctrl = all_findings[all_findings["Finding_Type"] != FINDING_CONTROL]
    if norm is not None and not norm.empty:
        flagged_exposure = compute_unique_flagged_exposure(findings, norm)
    else:
        flagged_exposure = float(
            non_ctrl.groupby("Line_Key", dropna=False)["Exposure_Amount"].max().sum()
        )

    ctrl = all_findings[all_findings["Finding_Type"] == FINDING_CONTROL]
    control_issue_exposure = float(
        ctrl.groupby("Line_Key", dropna=False)["Exposure_Amount"].max().sum()
    )

    recoverable_summary = (
        all_findings.loc[sav_mask]
        .groupby("Rule", dropna=False)
        .agg(
            Findings=("Line_Key", "count"),
            Recoverable_Amount=("Recoverable_Amount", "sum"),
        )
        .reset_index()
        .sort_values("Recoverable_Amount", ascending=False)
    )

    rule_summary = (
        all_findings.groupby(["Rule", "Finding_Type"], dropna=False)
        .agg(
            Findings=("Line_Key", "count"),
            Exposure=("Exposure_Amount", "sum"),
            Recoverable_Amount=("Recoverable_Amount", "sum"),
        )
        .reset_index()
        .sort_values(["Finding_Type", "Recoverable_Amount"], ascending=[True, False])
    )

    return {
        "all_findings": all_findings,
        "estimated_savings": round(estimated_savings, 2),
        "flagged_exposure": round(flagged_exposure, 2),
        "control_issue_exposure": round(control_issue_exposure, 2),
        "recoverable_summary": recoverable_summary,
        "rule_summary": rule_summary,
    }


def build_executive_summary(result: ManufacturingAuditResult) -> dict[str, Any]:
    """Structured executive payload for API / UI (three-way split)."""
    k = result.kpis
    return {
        "estimated_savings": float(k.get("estimated_savings_usd", 0) or 0),
        "flagged_exposure": float(k.get("flagged_exposure_usd", 0) or 0),
        "control_issue_exposure": float(k.get("control_issue_exposure_usd", 0) or 0),
        "message": (
            "Estimated savings reflects only recoverable items such as exact duplicates, "
            "tax variances, and validated price drift deltas. Flagged exposure includes "
            "review-only anomalies and should not be interpreted as direct cash recovery."
        ),
    }


def run_manufacturing_ap_audit(
    df_raw: pd.DataFrame,
    *,
    expected_tax_rate: float | None = None,
) -> ManufacturingAuditResult:
    colmap = _resolve_columns(df_raw)
    norm = normalize_ledger(df_raw, colmap)

    _inferred_fee: float | None = None
    if os.environ.get("CLEARSPEND_EMBEDDED_FEE_RATE") is None:
        _inferred_fee = infer_embedded_fee_rate(norm)

    findings = {
        "math_integrity": math_integrity_check(norm, embedded_fee_rate=_inferred_fee),
        "exact_duplicate": exact_duplicate_check(norm),
        "near_duplicate": near_duplicate_check(norm),
        "tax_variance": tax_variance_check(norm, expected_rate=expected_tax_rate),
        "price_drift": price_drift_check(norm),
        "currency": currency_consistency_check(norm),
        "weekend": weekend_posting_check(norm),
        "threshold": threshold_check(norm),
        "stale_invoice": stale_invoice_check(norm),
        "vendor_variation": vendor_name_variation_signal(norm),
    }

    consolidated = consolidate_findings(findings, norm=norm)
    exp = float(consolidated["flagged_exposure"])
    sav = float(consolidated["estimated_savings"])
    ctrl_exp = float(consolidated["control_issue_exposure"])

    spend = float(pd.to_numeric(norm["Total_Amount"], errors="coerce").fillna(0).sum())
    n_inv = max(len(norm), 1)
    mi_n = len(findings["math_integrity"]) if findings["math_integrity"] is not None else 0
    math_rate = mi_n / n_inv
    kpis = {
        "total_ledger_spend_usd": spend,
        "invoices_reviewed": int(len(norm)),
        "vendors_reviewed": int(norm["Vendor_ID"].nunique()),
        "flagged_exposure_usd": exp,
        "estimated_savings_usd": sav,
        "control_issue_exposure_usd": ctrl_exp,
        "confirmed_duplicate_rows": int(len(findings["exact_duplicate"])),
        "math_integrity_rows": mi_n,
        "math_integrity_flag_rate": round(math_rate, 4),
        "data_quality_issue_rows": int(
            len(findings["currency"])
            + len(findings["weekend"])
            + len(findings["threshold"])
            + len(findings["stale_invoice"])
            + len(findings["vendor_variation"])
        ),
    }
    if math_rate > 0.25 and n_inv > 30:
        kpis["math_integrity_mass_flag_warning"] = (
            "Over 25% of invoices failed Invoice+Tax vs Total — check column mapping, "
            "tax-inclusive vs exclusive totals, or set CLEARSPEND_MATH_TOLERANCE."
        )
    out = ManufacturingAuditResult(
        normalized=norm,
        findings=findings,
        flagged_exposure_usd=exp,
        estimated_savings_usd=sav,
        kpis=kpis,
    )
    out.kpis["executive_summary"] = build_executive_summary(out)
    return out


def build_export_report_md(result: ManufacturingAuditResult) -> str:
    """Full text report for download (markdown): KPIs, top vendors, rule row counts."""
    k = result.kpis
    lines = [
        "# ClearSpend — Manufacturing AP forensic report",
        "",
        build_executive_summary_md(result),
        "",
        "## Top flagged vendors (by unique invoice exposure)",
        "",
    ]
    tv = top_flagged_vendors_by_exposure(result, top_n=15)
    if tv.empty:
        lines.append("_No flagged invoices._")
    else:
        for _, r in tv.iterrows():
            lines.append(
                f"- **{r['Vendor_Name']}** (`{r['Vendor_ID']}`): "
                f"${float(r['Flagged_Exposure_USD']):,.2f} — {int(r['Invoice_Count'])} invoice(s)"
            )
    lines.extend(
        [
            "",
            "## Rule hit counts",
            "",
        ]
    )
    for rule, fdf in sorted(result.findings.items()):
        n = len(fdf) if fdf is not None else 0
        lines.append(f"- **{rule}:** {n} rows")
    lines.append("")
    lines.append(
        "_Estimated savings = exact duplicate extras + tax variance + price drift deltas only "
        "(max recoverable per invoice, then sum). Flagged exposure = non-control findings, max exposure per line key. "
        "Control issues = separate exposure and row counts — not savings._"
    )
    return "\n".join(lines)


def build_executive_summary_md(result: ManufacturingAuditResult) -> str:
    """Static executive summary from Python KPIs (no LLM)."""
    k = result.kpis
    lines = [
        "## Executive summary",
        "",
        f"- **Total ledger spend:** ${k.get('total_ledger_spend_usd', 0):,.2f}",
        f"- **Invoices reviewed:** {k.get('invoices_reviewed', 0):,}",
        f"- **Vendors reviewed:** {k.get('vendors_reviewed', 0):,}",
        "",
        "### Estimated savings (cash-like, conservative)",
        f"- **~${k.get('estimated_savings_usd', 0):,.2f}** — **only** from:",
        "  - **Exact duplicate payments** (extra rows after the first in each vendor/date/amount cluster),",
        "  - **Tax variance** (absolute delta vs dataset median implied rate or ``CLEARSPEND_EXPECTED_TAX_RATE``),",
        "  - **Price drift** — **estimated sourcing opportunity** (pre-tax excess above peer **median** "
        "by vendor + category, using within-group dispersion; recoverable is **delta only**, never full invoice).",
        "- If one invoice hits multiple savings rules, we take the **largest single recoverable** for that invoice, then sum (no double counting).",
        "",
        "### Flagged exposure (review scope in dollars)",
        f"- **${k.get('flagged_exposure_usd', 0):,.2f}** — max **ledger Total_Amount** per line (invoice or near-dup pair key) "
        "across **non-control** hits: math integrity, substantive rules, near-duplicate review, **including** "
        "reference rows in duplicate clusters. **Not** cash recovery; do not add to estimated savings.",
        "",
        "### Control issues (review only — not savings)",
        f"- **${k.get('control_issue_exposure_usd', 0):,.2f}** — dollar exposure for selected control flags "
        f"(near-threshold, currency coding, payment lag); weekend dates and vendor-name variations are row-level "
        f"only and excluded from this total. **{k.get('data_quality_issue_rows', 0)}** control row(s) overall.",
        "",
        f"- **Exact-duplicate cluster rows:** {k.get('confirmed_duplicate_rows', 0)}",
        "",
        "### Math integrity",
        "- **Cent-rounded** check: `Total_Amount` vs `(Invoice_Amount + Tax) × (1 + fee)` (fee **0** unless "
        "`CLEARSPEND_EMBEDDED_FEE_RATE` is set, or the run **infers** a consistent median surcharge when "
        "that env var is unset and there are enough rows). Tolerance: **$0.10** baseline; "
        "`CLEARSPEND_MATH_TOLERANCE`, optional 1¢ cushion via `CLEARSPEND_MATH_EXTRA_PENNY=1`. "
        "**Review Required** — not included in estimated savings. If **mass-flag warning** appears, "
        "adjust fee/tolerance or column definitions.",
        "",
        "_Numbers are computed in Python; use rule tabs for detail._",
    ]
    return "\n".join(lines)


def compact_context_for_llm(result: ManufacturingAuditResult, *, max_example_rows: int = 10) -> str:
    """Small bundle for the LLM — KPIs, rule counts, tiny samples (not the full ledger)."""
    lines: list[str] = ["## Manufacturing AP audit (Python-computed)", "", "### KPIs"]
    for k, v in result.kpis.items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("### Top flagged vendors (by exposure)")
    tv = top_flagged_vendors_by_exposure(result, top_n=8)
    if tv.empty:
        lines.append("- (none)")
    else:
        lines.append(tv.to_json(orient="records"))
    lines.append("")
    lines.append("### Rule hit counts")
    for rule, fdf in sorted(result.findings.items()):
        n = len(fdf) if fdf is not None else 0
        lines.append(f"- {rule}: {n} rows")
    lines.append("")
    lines.append("### Top savings-eligible samples (max {})".format(max_example_rows))
    ed = result.findings.get("exact_duplicate")
    if ed is not None and not ed.empty:
        lines.append("Exact duplicate (head):")
        lines.append(ed.head(5)[["Invoice_ID", "Vendor_ID", "Total_Amount", "Recoverable_Amount"]].to_json(orient="records"))
    tv = result.findings.get("tax_variance")
    if tv is not None and not tv.empty:
        lines.append("Tax variance (head):")
        lines.append(tv.head(5)[["Invoice_ID", "Tax_Delta", "Recoverable_Amount"]].to_json(orient="records"))
    pd = result.findings.get("price_drift")
    if pd is not None and not pd.empty:
        lines.append("Price drift (head):")
        lines.append(pd.head(5)[["Invoice_ID", "Vendor_ID", "Price_Delta", "Recoverable_Amount"]].to_json(orient="records"))

    lines.append("")
    lines.append("### Reporting rules")
    lines.append(
        "- Estimated savings = exact duplicates + tax variance + price drift only (deduped per invoice max)."
    )
    lines.append(
        "- Flagged exposure = non-control findings: max Total_Amount (or pair exposure) per consolidated line key."
    )
    lines.append(
        "- Control issue exposure = separate sum of max exposure per line for control rules; not savings."
    )
    lines.append("- Price drift in narrative: estimated sourcing opportunity, not proven overbill.")
    return "\n".join(lines)[:8000]

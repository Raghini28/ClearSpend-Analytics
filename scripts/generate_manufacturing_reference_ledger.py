#!/usr/bin/env python3
"""
Generate sample_data/manufacturing_ap_reference_ledger.csv (~500 rows) with planted
issues so run_manufacturing_ap_audit() yields estimated_savings_usd ≈ $15,000.

Run from repo root: python scripts/generate_manufacturing_reference_ledger.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import manufacturing_ap_audit as mfg  # noqa: E402

OUT_PATH = ROOT / "sample_data" / "manufacturing_ap_reference_ledger.csv"

CATEGORIES = [
    "Raw Materials",
    "Logistics",
    "MRO",
    "Utilities",
    "Capex",
    "Packaging",
    "IT Services",
    "Facilities",
]
VENDORS = [
    ("V-001", "Gamma Supplies Ltd"),
    ("V-002", "Northwind Logistics Inc"),
    ("V-003", "Sterling MRO Co"),
    ("V-004", "Gridline Utilities LLC"),
    ("V-005", "Pacific Packaging"),
    ("V-006", "Vertex IT Services"),
    ("V-007", "Harbor Facilities Group"),
    ("V-008", "Capex Solutions Ltd"),
]

RNG = np.random.default_rng(42)
COST_CENTERS = [f"CC-{i:03d}" for i in range(1, 21)]
# Stable “contract pricing” per (vendor, category) so price-drift z-scores stay quiet in bulk.
_AMT_BASE: dict[tuple[str, str], float] = {}


def _amount_for_vendor_category(vid: str, cat: str) -> float:
    key = (vid, cat)
    if key not in _AMT_BASE:
        _AMT_BASE[key] = float(RNG.uniform(1800.0, 7500.0))
    base = _AMT_BASE[key]
    return round(base * (1.0 + float(RNG.uniform(-0.025, 0.025))), 2)


def _row(
    inv: str,
    v_id: str,
    v_name: str,
    cat: str,
    inv_date: str,
    pay_date: str,
    currency: str,
    inv_amt: float,
    tax: float | None = None,
    payment_status: str = "Paid",
    desc: str = "Purchase",
) -> dict:
    if tax is None:
        tax = round(float(inv_amt) * 0.08, 2)
    total = round(float(inv_amt) + float(tax), 2)
    return {
        "Invoice_ID": inv,
        "Vendor_Name": v_name,
        "Vendor_ID": v_id,
        "Category": cat,
        "Invoice_Date": inv_date,
        "Payment_Date": pay_date,
        "Currency": currency,
        "Invoice_Amount": inv_amt,
        "Tax": tax,
        "Total_Amount": total,
        "Payment_Status": payment_status,
        "Cost_Center": RNG.choice(COST_CENTERS),
        "Description": desc,
    }


def main() -> None:
    rows: list[dict] = []
    n_seq = 0

    def next_inv(prefix: str = "INV") -> str:
        nonlocal n_seq
        n_seq += 1
        return f"{prefix}-{n_seq:05d}"

    # --- Bulk “healthy” ledger (pseudo-ERP) ---
    # Exclude V-001 + Raw Materials from bulk so peer stats stay clean for planted drift.
    for month in range(24):
        y, m = 2024 + month // 12, month % 12 + 1
        for _ in range(19):
            vid, vname = VENDORS[int(RNG.integers(0, len(VENDORS)))]
            cat = str(RNG.choice(CATEGORIES))
            if vid == "V-001" and cat == "Raw Materials":
                cat = "MRO"
            amt = _amount_for_vendor_category(vid, cat)
            day = int(RNG.integers(1, 28))
            pay_d = min(day + int(RNG.integers(5, 25)), 28)
            rows.append(
                _row(
                    next_inv(),
                    vid,
                    vname,
                    cat,
                    f"{y}-{m:02d}-{day:02d}",
                    f"{y}-{m:02d}-{pay_d:02d}",
                    "USD",
                    amt,
                )
            )

    def _tax8(amt: float) -> float:
        return round(float(amt) * 0.08, 2)

    # --- A: Exact duplicates (recoverable on duplicate rows only; tuned with tax+drift ≈ $15k) ---
    # Pair A: 2800 + 224 = 3024 (Logistics — avoid V-001 Raw peer cohort used for drift)
    rows.append(
        _row(
            "INV-DUP-A1",
            "V-002",
            "Northwind Logistics Inc",
            "Logistics",
            "2024-03-15",
            "2024-03-22",
            "USD",
            2800.0,
            224.0,
            desc="Freight — duplicate payment risk (A)",
        )
    )
    rows.append(
        _row(
            "INV-DUP-A1A",
            "V-002",
            "Northwind Logistics Inc",
            "Logistics",
            "2024-03-15",
            "2024-03-23",
            "USD",
            2800.0,
            224.0,
            desc="Freight — dup ID variant",
        )
    )

    # Pair B: 2500 + 200 = 2700
    rows.append(
        _row(
            "INV-DUP-B1",
            "V-003",
            "Sterling MRO Co",
            "MRO",
            "2024-07-10",
            "2024-07-18",
            "USD",
            2500.0,
            200.0,
            desc="Bearings batch B",
        )
    )
    rows.append(
        _row(
            "INV-DUP-B1A",
            "V-003",
            "Sterling MRO Co",
            "MRO",
            "2024-07-10",
            "2024-07-19",
            "USD",
            2500.0,
            200.0,
            desc="Bearings batch B — dup",
        )
    )

    # Triplicate C: 1900 + 152 = 2052 × 2 extra = 4104
    rows.append(
        _row(
            "INV-DUP-C1",
            "V-005",
            "Pacific Packaging",
            "Packaging",
            "2024-11-05",
            "2024-11-12",
            "USD",
            1900.0,
            152.0,
            desc="Corrugated — triplicate pay",
        )
    )
    rows.append(
        _row(
            "INV-DUP-C2",
            "V-005",
            "Pacific Packaging",
            "Packaging",
            "2024-11-05",
            "2024-11-13",
            "USD",
            1900.0,
            152.0,
            desc="Corrugated — triplicate pay",
        )
    )
    rows.append(
        _row(
            "INV-DUP-C3",
            "V-005",
            "Pacific Packaging",
            "Packaging",
            "2024-11-05",
            "2024-11-14",
            "USD",
            1900.0,
            152.0,
            desc="Corrugated — triplicate pay",
        )
    )

    # --- B: Tax variance (flat 8% benchmark; over-charged tax, ~$2.5k total delta) ---
    tax_cases = [
        ("INV-TAX-01", "V-002", "Northwind Logistics Inc", "Logistics", 4000.0, _tax8(4000.0) + 520),
        ("INV-TAX-02", "V-004", "Gridline Utilities LLC", "Utilities", 3500.0, _tax8(3500.0) + 515),
        ("INV-TAX-03", "V-006", "Vertex IT Services", "IT Services", 3200.0, _tax8(3200.0) + 510),
        ("INV-TAX-04", "V-007", "Harbor Facilities Group", "Facilities", 2800.0, _tax8(2800.0) + 505),
        ("INV-TAX-05", "V-008", "Capex Solutions Ltd", "Capex", 3600.0, _tax8(3600.0) + 500),
    ]
    for inv, vid, vname, cat, amt, bad_tax in tax_cases:
        rows.append(
            _row(
                inv,
                vid,
                vname,
                cat,
                "2024-09-12",
                "2024-09-20",
                "USD",
                amt,
                tax=round(float(bad_tax), 2),
                desc="Tax rounding / rate mismatch (planted)",
            )
        )

    # --- C: Price drift cohort — tight peers + one outlier (~$4.2k delta vs median) ---
    peer_cat = "Raw Materials"
    peer_vid, peer_name = "V-001", "Gamma Supplies Ltd"
    # At least 20 peers so price-drift min_peer_count (code default) includes planted outlier.
    baseline_peers = [1188.0 + i * 2.0 for i in range(24)]
    for i, pamt in enumerate(baseline_peers):
        rows.append(
            _row(
                f"INV-PEER-{i:02d}",
                peer_vid,
                peer_name if i % 2 == 0 else "Gamma Supply Ltd",
                peer_cat,
                f"2024-04-{(i % 27) + 1:02d}",
                f"2024-05-{(i % 27) + 1:02d}",
                "USD",
                pamt,
                desc="Peer baseline line",
            )
        )
    rows.append(
        _row(
            "INV-DRIFT-OUT",
            peer_vid,
            peer_name,
            peer_cat,
            "2024-04-18",
            "2024-05-02",
            "USD",
            3820.0,
            desc="Inflated vs peers (planted drift)",
        )
    )

    # --- D: Control signals only (no savings) ---
    rows.append(
        _row(
            "INV-USD-ALT",
            "V-002",
            "Northwind Logistics Inc",
            "Logistics",
            "2024-02-14",
            "2024-02-21",
            "US$",
            725.5,
            desc="Currency code variant",
        )
    )
    # Weekend invoice date
    rows.append(
        _row(
            "INV-WEEKEND",
            "V-007",
            "Harbor Facilities Group",
            "Facilities",
            "2024-06-15",
            "2024-06-20",
            "USD",
            1840.0,
            desc="Weekend-dated invoice",
        )
    )
    # Near approval threshold — unique Vendor_ID so it does not distort price-drift peers
    rows.append(
        _row(
            "INV-THRESH",
            "V-099",
            "Gridline Utilities LLC",
            "Utilities",
            "2024-08-01",
            "2024-08-10",
            "USD",
            50240.0,
            round(50240.0 * 0.08, 2),
            desc="Near approval tier (control signal)",
        )
    )
    # Light math mismatch (exposure / fix, not in savings KPI)
    tax_ok = round(888.0 * 0.08, 2)
    rows.append(
        {
            "Invoice_ID": "INV-MATH-01",
            "Vendor_Name": "Sterling MRO Co",
            "Vendor_ID": "V-003",
            "Category": "MRO",
            "Invoice_Date": "2024-05-22",
            "Payment_Date": "2024-05-30",
            "Currency": "USD",
            "Invoice_Amount": 888.0,
            "Tax": tax_ok,
            "Total_Amount": round(888.0 + tax_ok, 2) + 2.0,
            "Payment_Status": "Paid",
            "Cost_Center": "CC-014",
            "Description": "Small total mismatch (planted)",
        }
    )

    # --- E: Near duplicate pair (review only) ---
    rows.append(
        _row(
            "INV-NEAR-1",
            "V-007",
            "Harbor Facilities Group",
            "Facilities",
            "2024-10-02",
            "2024-10-09",
            "USD",
            3300.0,
            desc="Near-dup pair A",
        )
    )
    rows.append(
        _row(
            "INV-NEAR-2",
            "V-007",
            "Harbor Facilities Group",
            "Facilities",
            "2024-10-04",
            "2024-10-11",
            "USD",
            3302.0,
            desc="Near-dup pair B",
        )
    )

    # Pad to ~500 rows if needed (bulk is deterministic size).
    while len(rows) < 500:
        vid, vname = VENDORS[int(RNG.integers(0, len(VENDORS)))]
        cat = str(RNG.choice(CATEGORIES))
        if vid == "V-001" and cat == "Raw Materials":
            cat = "MRO"
        rows.append(
            _row(
                next_inv(),
                vid,
                vname,
                cat,
                "2025-03-10",
                "2025-03-20",
                "USD",
                _amount_for_vendor_category(vid, cat),
            )
        )

    df = pd.DataFrame(rows)
    df = df.sort_values("Invoice_Date").reset_index(drop=True)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT_PATH, index=False)

    r = mfg.run_manufacturing_ap_audit(df)
    print("Wrote", OUT_PATH)
    print("Rows:", len(df))
    print("Estimated savings USD:", round(r.estimated_savings_usd, 2))
    print("Flagged exposure USD:", round(r.flagged_exposure_usd, 2))


if __name__ == "__main__":
    main()

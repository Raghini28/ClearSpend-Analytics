"""Financial exposure should not treat every row as full-invoice risk."""

import pandas as pd
import pytest

import manufacturing_ap_audit as mfg


def test_math_cent_rounding_no_false_positive_from_float_noise():
    df = pd.DataFrame(
        [
            {
                "Invoice_ID": "F1",
                "Vendor_Name": "Acme",
                "Vendor_ID": "V1",
                "Category": "MRO",
                "Invoice_Date": "2024-01-10",
                "Payment_Date": "2024-01-15",
                "Currency": "USD",
                "Invoice_Amount": 1000.0,
                "Tax": 80.0,
                "Total_Amount": 1080.0 + 1e-12,
                "Payment_Status": "Paid",
                "Cost_Center": "CC1",
                "Description": "fp noise",
            }
        ]
    )
    r = mfg.run_manufacturing_ap_audit(df)
    assert r.findings["math_integrity"].empty


def test_control_rules_do_not_inflate_dollar_exposure():
    """Weekend/currency/threshold/vendor_variation must not sum invoice totals into exposure."""
    rows = []
    for i in range(5):
        rows.append(
            {
                "Invoice_ID": f"INV-{i}",
                "Vendor_Name": "Acme",
                "Vendor_ID": "V1",
                "Category": "MRO",
                "Invoice_Date": f"2024-06-{14 + i:02d}",
                "Payment_Date": "2024-06-20",
                "Currency": "US$",
                "Invoice_Amount": 100_000.0 + 100.0 * i,
                "Tax": round(0.08 * (100_000.0 + 100.0 * i), 2),
                "Total_Amount": round(
                    (100_000.0 + 100.0 * i) + round(0.08 * (100_000.0 + 100.0 * i), 2),
                    2,
                ),
                "Payment_Status": "Paid",
                "Cost_Center": "CC1",
                "Description": "x",
            }
        )
    df = pd.DataFrame(rows)
    r = mfg.run_manufacturing_ap_audit(df)
    # All rows hit currency + weekend; financial exposure should be ~0
    assert r.flagged_exposure_usd == 0.0
    assert r.kpis["data_quality_issue_rows"] > 0


def test_math_exposure_uses_invoice_total_for_review_scope():
    df = pd.DataFrame(
        [
            {
                "Invoice_ID": "M1",
                "Vendor_Name": "Acme",
                "Vendor_ID": "V1",
                "Category": "MRO",
                "Invoice_Date": "2024-01-10",
                "Payment_Date": "2024-01-15",
                "Currency": "USD",
                "Invoice_Amount": 1000.0,
                "Tax": 80.0,
                "Total_Amount": 1085.0,
                "Payment_Status": "Paid",
                "Cost_Center": "CC1",
                "Description": "math off",
            }
        ]
    )
    r = mfg.run_manufacturing_ap_audit(df)
    mi = r.findings["math_integrity"]
    assert len(mi) == 1
    exp = r.flagged_exposure_usd
    assert exp == 1085.0
    assert abs(float(mi.iloc[0]["Math_Delta"])) > 0.10


def test_price_drift_exposure_is_delta_only():
    base_rows = []
    for i in range(35):
        base_rows.append(
            {
                "Invoice_ID": f"P{i:02d}",
                "Vendor_Name": "PeerCo",
                "Vendor_ID": "VP",
                "Category": "Raw Materials",
                "Invoice_Date": f"2024-02-{(i % 28) + 1:02d}",
                "Payment_Date": f"2024-03-{(i % 28) + 1:02d}",
                "Currency": "USD",
                "Invoice_Amount": 1000.0 + i * 2.0,
                "Tax": 80.0 + i * 0.16,
                "Total_Amount": 1080.0 + i * 2.16,
                "Payment_Status": "Paid",
                "Cost_Center": "CC1",
                "Description": "peer",
            }
        )
    base_rows.append(
        {
            "Invoice_ID": "P-OUT",
            "Vendor_Name": "PeerCo",
            "Vendor_ID": "VP",
            "Category": "Raw Materials",
            "Invoice_Date": "2024-02-20",
            "Payment_Date": "2024-03-05",
            "Currency": "USD",
            "Invoice_Amount": 5000.0,
            "Tax": 400.0,
            "Total_Amount": 5400.0,
            "Payment_Status": "Paid",
            "Cost_Center": "CC1",
            "Description": "outlier",
        }
    )
    df = pd.DataFrame(base_rows)
    r = mfg.run_manufacturing_ap_audit(df)
    pdv = r.findings["price_drift"]
    assert not pdv.empty
    inv_exp = mfg.accumulate_unique_invoice_exposure_usd(r.findings, r.normalized)
    rec = float(pdv[pdv["Invoice_ID"] == "P-OUT"]["Recoverable_Amount"].iloc[0])
    tot = float(pdv[pdv["Invoice_ID"] == "P-OUT"]["Total_Amount"].iloc[0])
    assert inv_exp.get("P-OUT", 0) == tot
    assert rec > 0 and rec < tot
    assert rec == pytest.approx(float(pdv[pdv["Invoice_ID"] == "P-OUT"]["Price_Delta"].iloc[0]))

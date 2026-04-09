import pandas as pd

import manufacturing_ap_audit as mfg


def test_schema_detection_and_dup_savings():
    df = pd.read_csv("tests/fixtures/manufacturing_mini.csv")
    assert mfg.has_manufacturing_schema(df)
    r = mfg.run_manufacturing_ap_audit(df)
    assert r.kpis["invoices_reviewed"] == 3
    ed = r.findings["exact_duplicate"]
    assert len(ed) == 2
    assert float(ed["Recoverable_Amount"].sum()) > 0
    assert r.kpis["estimated_savings_usd"] >= 0


def test_exact_duplicate_savings_not_double_counted_for_pair():
    """Two-row duplicate cluster: recoverable should be one Total_Amount, not two."""
    df = pd.read_csv("tests/fixtures/manufacturing_mini.csv")
    r = mfg.run_manufacturing_ap_audit(df)
    ed = r.findings["exact_duplicate"]
    assert len(ed) == 2
    assert float(ed["Recoverable_Amount"].sum()) == float(ed["Total_Amount"].max())
    assert r.estimated_savings_usd == float(ed["Recoverable_Amount"].sum())


def test_savings_only_from_savings_rules():
    edf = pd.DataFrame([{"Invoice_ID": "A", "Recoverable_Amount": 100.0}])
    edf["Counts_Toward_Savings"] = True
    tdf = pd.DataFrame([{"Invoice_ID": "B", "Recoverable_Amount": 50.0}])
    tdf["Counts_Toward_Savings"] = True
    mdf = pd.DataFrame([{"Invoice_ID": "C", "Recoverable_Amount": 999.0}])
    mdf["Counts_Toward_Savings"] = False
    acc = {
        "exact_duplicate": edf,
        "tax_variance": tdf,
        "math_integrity": mdf,
    }
    assert mfg.compute_estimated_savings_deduped(acc) == 150.0

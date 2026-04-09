"""Reference manufacturing ledger tuned to ~$15k estimated savings."""

from pathlib import Path

import pandas as pd
import pytest

import manufacturing_ap_audit as mfg

REF = Path(__file__).resolve().parents[1] / "sample_data" / "manufacturing_ap_reference_ledger.csv"


@pytest.mark.skipif(not REF.is_file(), reason="reference ledger not generated yet")
def test_reference_ledger_schema_and_savings_band():
    df = pd.read_csv(REF)
    assert mfg.has_manufacturing_schema(df)
    assert len(df) >= 495
    r = mfg.run_manufacturing_ap_audit(df)
    # Band allows stricter price drift (z=3, min group 30) and median-implied tax baseline.
    assert 11_500 < r.estimated_savings_usd < 16_500
    assert r.kpis["confirmed_duplicate_rows"] >= 2


def test_top_vendors_helper_empty_ok():
    df = pd.read_csv("tests/fixtures/manufacturing_mini.csv")
    r = mfg.run_manufacturing_ap_audit(df)
    tv = mfg.top_flagged_vendors_by_exposure(r, top_n=5)
    assert "Vendor_ID" in tv.columns or tv.empty


import pandas as pd

from audit_engine import apply_ignored_vendors_to_result, prepare_ledger


def test_ignored_strip_flagged_rows():
    df = pd.DataFrame(
        {
            "Amount": [100.0, 200.0],
            "Vendor": ["Acme Corp", "Bad Guy LLC"],
            "id": ["1", "2"],
        }
    )
    ctx = prepare_ledger(df)
    assert not ctx.get("error")
    ctx["ignored_vendors"] = {"bad guy llc"}
    base = {
        "flagged_rows": [
            {"__V": "Acme Corp", "__L": 100.0},
            {"__V": "Bad Guy LLC", "__L": 200.0},
        ],
        "flagged_row_count": 2,
        "exposure_usd": 300.0,
        "by_vendor": [
            {"vendor": "Acme Corp", "exposure_line_amount": 100},
            {"vendor": "Bad Guy LLC", "exposure_line_amount": 200},
        ],
    }
    out = apply_ignored_vendors_to_result(ctx, base)
    assert len(out["flagged_rows"]) == 1
    assert out["exposure_usd"] == 100.0

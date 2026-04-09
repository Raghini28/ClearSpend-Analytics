"""Deduplicated exposure: same ledger line flagged by two tools counts once."""

from audit_engine import unique_flagged_line_exposure_from_accumulated


def test_same_row_two_tools_once():
    acc = {
        "check_math_integrity": {
            "error": None,
            "flagged_rows": [
                {"__ROW_IDX": 5, "__ID": "A", "__V": "V", "__L": 100.0, "__D": "2024-01-01"},
            ],
        },
        "find_duplicate_invoices": {
            "error": None,
            "flagged_rows": [
                {"__ROW_IDX": 5, "__ID": "A", "__V": "V", "__L": 100.0, "__D": "2024-01-01"},
            ],
        },
    }
    total, n = unique_flagged_line_exposure_from_accumulated(acc)
    assert n == 1
    assert total == 100.0


def test_two_distinct_rows():
    acc = {
        "a": {
            "error": None,
            "flagged_rows": [
                {"__ROW_IDX": 0, "__L": 50.0, "__ID": "1", "__V": "x", "__D": "2024-01-01"},
                {"__ROW_IDX": 1, "__L": 50.0, "__ID": "2", "__V": "x", "__D": "2024-01-01"},
            ],
        },
    }
    total, n = unique_flagged_line_exposure_from_accumulated(acc)
    assert n == 2
    assert total == 100.0

import pytest

from services.upload_validate import UploadValidationError, read_ledger_bytes


def test_read_csv_ok():
    raw = b"a,b,c\n1,2,3\n4,5,6\n"
    df = read_ledger_bytes(raw, "t.csv")
    assert len(df) == 2
    assert list(df.columns) == ["a", "b", "c"]


def test_empty_csv_errors():
    raw = b"a\n"
    with pytest.raises(UploadValidationError):
        read_ledger_bytes(raw, "t.csv")


def test_bad_extension():
    with pytest.raises(UploadValidationError):
        read_ledger_bytes(b"x", "f.txt")

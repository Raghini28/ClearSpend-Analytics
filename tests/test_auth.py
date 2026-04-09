from services import auth


def test_hash_verify_roundtrip():
    h = auth._hash_password("secret-value")
    assert auth._verify_hash("secret-value", h)
    assert not auth._verify_hash("wrong", h)


def test_verify_legacy_plaintext():
    rec = {"pw": "hello"}
    assert auth.verify_user_password(rec, "hello")
    assert not auth.verify_user_password(rec, "no")


def test_tool_confidence_exported_via_audit_engine():
    from audit_engine import TOOL_CONFIDENCE

    assert TOOL_CONFIDENCE["check_math_integrity"] == "High"

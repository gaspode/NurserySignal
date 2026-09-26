from app.correlation import classify_match, preferred_match_reason
from app.repository import _safe_system_duplicate_key


def test_preferred_match_reason_uses_match_decision_outcome():
    postcode_match = classify_match("BS1 1AA", "Little Acorns", "BS1 1AA", "Little Acorns")
    operator_match = classify_match("BS1 1AA", "Acorns", "BS1 1AA", "Little Acorns")

    assert postcode_match.outcome == "EXACT"
    assert preferred_match_reason(postcode_match, operator_match) == postcode_match.reason


def test_preferred_match_reason_falls_back_to_operator_match():
    postcode_match = classify_match("BS1 1AA", "Unrelated", "BS1 1AA", "Little Acorns")
    operator_match = classify_match("BS1 1AA", "Little Acorns", "BS1 1AA", "Little Acorns")

    assert postcode_match.outcome == "UNCERTAIN"
    assert preferred_match_reason(postcode_match, operator_match) == operator_match.reason


def test_system_duplicate_key_requires_strong_identity_fields():
    row = ("op-1", "NURSERY", "AL1 1JD", "Orient Close Nursery", "EXPANSION", None)
    assert _safe_system_duplicate_key(row) == (
        "NURSERY",
        "al1 1jd",
        "orient close nursery",
        "EXPANSION",
    )
    assert (
        _safe_system_duplicate_key(("op-2", "NURSERY", "AL1 1JD", None, "EXPANSION", None))
        is None
    )

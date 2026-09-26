from app.correlation import classify_match, preferred_match_reason
from app.repository import _duplicate_group_keys, _safe_system_duplicate_key


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
    row = ("op-1", "NURSERY", "AL1 1JD", "Orient Close Nursery", "EXPANSION", None, None)
    assert _safe_system_duplicate_key(row) == (
        "NURSERY",
        "al1 1jd",
        "operator",
        "orient close nursery",
        "EXPANSION",
    )
    assert (
        _safe_system_duplicate_key(
            ("op-2", "NURSERY", "AL1 1JD", None, "EXPANSION", None, "Orient Close Nursery")
        )
        == ("NURSERY", "al1 1jd", "site", "orient close nursery", "EXPANSION")
    )
    assert (
        _safe_system_duplicate_key(
            ("op-3", "NURSERY", "AL1 1JD", None, "EXPANSION", None, None)
        )
        is None
    )


def test_system_duplicate_key_normalizes_identity_but_rejects_conflicts():
    first = ("op-1", "NURSERY", "AL1-1JD", "Orient Close, Nursery", "EXPANSION", None, None)
    equivalent = ("op-2", "NURSERY", "al1 1jd", "orient close nursery", "EXPANSION", None, None)
    different_operator = ("op-3", "NURSERY", "AL1 1JD", "Other Nursery", "EXPANSION", None, None)
    different_postcode = (
        "op-4",
        "NURSERY",
        "AL2 2JD",
        "Orient Close Nursery",
        "EXPANSION",
        None,
        None,
    )

    assert _safe_system_duplicate_key(first) == _safe_system_duplicate_key(equivalent)
    assert _safe_system_duplicate_key(first) != _safe_system_duplicate_key(different_operator)
    assert _safe_system_duplicate_key(first) != _safe_system_duplicate_key(different_postcode)


def test_duplicate_group_keys_include_exact_shared_signal_identity():
    row = (
        "op-1",
        "NURSERY",
        "AL1 1JD",
        None,
        "EXPANSION",
        None,
        "Orient Close Nursery",
        ["signal-1"],
    )

    assert ("NURSERY", "shared_signal", "signal-1", "EXPANSION", "") in _duplicate_group_keys(row)

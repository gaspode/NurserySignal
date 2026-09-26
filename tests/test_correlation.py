from app.correlation import classify_match, preferred_match_reason


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

from datetime import UTC, datetime

from app.repository import _care_withdrawal_preview_from_inventory


def _item(
    opportunity_id: str,
    *,
    lifecycle: str,
    published: bool = True,
    automatic: bool = True,
    outcome: str = "Approved",
    hygiene: str = "VALID_SUPPORTED",
) -> dict:
    signal = {
        "id": f"signal-{opportunity_id}",
        "source_type": "planning",
        "status": "ACTIVE",
        "metadata": {"decision": outcome},
        "discovered_at": datetime(2026, 9, 1, tzinfo=UTC),
    }
    opportunity = {
        "id": opportunity_id,
        "publication_status": "PUBLISHED" if published else "DRAFT",
        "customer_lifecycle_stage": lifecycle,
        "publication_automation_blocked": False,
        "publication_automation_provenance": (
            {"policy_version": "care-publication-v3"} if automatic else {}
        ),
        "customer_published_at": datetime(2026, 9, 2, tzinfo=UTC),
        "relationships": [signal],
    }
    return {
        "opportunity": opportunity,
        "projection": dict(opportunity),
        "hygiene": {
            "category": hygiene,
            "warning": None,
            "foundational_signal_count": 1,
            "supporting_signal_count": 0,
            "source_types": ["planning"],
        },
        "safe_title": "New children's home — Nottingham, NG8",
        "safe_summary": "Planning evidence is available.",
        "decision": None,
    }


def test_withdrawal_preview_is_published_only_and_read_only() -> None:
    inventory = {
        "decisions": [
            _item("auto-stopped", lifecycle="STOPPED", outcome="Refused"),
            _item("manual-stopped", lifecycle="STOPPED", automatic=False, outcome="Refused"),
            _item("auto-active", lifecycle="PLANNING_APPROVED"),
            _item("draft", lifecycle="STOPPED", published=False, outcome="Refused"),
        ]
    }
    preview = _care_withdrawal_preview_from_inventory(inventory)

    assert preview["preview_only"] is True
    assert preview["enabled"] is False
    assert preview["publication_state_mutations"] == 0
    assert preview["currently_published_total"] == 3
    assert preview["outcomes"] == {
        "KEEP_PUBLISHED": 1,
        "AUTO_WITHDRAW_ELIGIBLE": 1,
        "MANUAL_REVIEW": 0,
        "MANUAL_PROTECTION": 1,
    }
    assert preview["reason_counts"] == {
        "current_evidence_remains_active": 1,
        "manual_publication_protection": 1,
        "planning_refused": 1,
    }
    assert preview["automatic_withdrawal_candidates"][0]["opportunity_id"] == "auto-stopped"
    assert preview["manually_protected_terminal_cases"][0]["opportunity_id"] == (
        "manual-stopped"
    )


def test_withdrawal_preview_is_deterministic_and_bounded() -> None:
    inventory = {
        "decisions": [
            _item(f"stopped-{index}", lifecycle="STOPPED", outcome="Withdrawn")
            for index in range(110)
        ]
    }
    first = _care_withdrawal_preview_from_inventory(inventory)
    second = _care_withdrawal_preview_from_inventory(inventory)
    assert first == second
    assert first["outcomes"]["AUTO_WITHDRAW_ELIGIBLE"] == 110
    assert len(first["automatic_withdrawal_candidates"]) == 100
    assert len(first["samples"]["AUTO_WITHDRAW_ELIGIBLE"]) == 5

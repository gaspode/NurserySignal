from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.care_lifecycle import (
    CARE_PUBLICATION_HOLDOUT_VERSION,
    CARE_PUBLICATION_POLICY_VERSION,
    CARE_WITHDRAWAL_POLICY_VERSION,
    CareLifecycle,
    bootstrap_lifecycle_records,
    classify_publication_conflict,
    derive_care_lifecycle,
    deterministic_initial_poll_at,
    deterministic_policy_samples,
    evaluate_planning_watch,
    evaluate_publication,
    evaluate_publication_v2,
    evaluate_withdrawal,
    project_planning_watch_requests,
    publication_qa_holdout,
)


def planning(
    outcome: str,
    *,
    signal_id: str = "planning-1",
    review_status: str = "APPROVED",
    decision: str = "CREATE_OPPORTUNITY",
    subtype: str = "NEW_HOME_CHANGE_OF_USE",
) -> dict:
    return {
        "id": signal_id,
        "external_id": "26/00123/FUL",
        "source_type": "planning",
        "relationship_status": "ACTIVE",
        "review_status": review_status,
        "metadata": {"decision": outcome, "local_authority": "Nottingham"},
        "extracted_facts": {
            "planning_subtype": subtype,
            "opportunity_creation_decision": decision,
        },
    }


def opportunity(**overrides) -> dict:
    return {
        "id": "00000000-0000-0000-0000-000000000001",
        "vertical": "CHILDRENS_HOME",
        "publication_status": "DRAFT",
        "change_type": "OPENING",
        "town": "Nottingham",
        "postcode": "NG8 1LD",
        "address": "10 Kingswood Road",
        "operator_name": "Example Care Ltd",
        "publication_automation_blocked": False,
        **overrides,
    }


def watch_signal(
    now: datetime,
    *,
    outcome: str = "Pending",
    age_days: int = 10,
    review_status: str = "APPROVED",
    decision: str = "CREATE_OPPORTUNITY",
    planning_status: str | None = None,
) -> dict:
    signal = planning(outcome, review_status=review_status, decision=decision)
    signal["discovered_at"] = (now - timedelta(days=age_days)).isoformat()
    signal["metadata"].update(
        {
            "application_date": (now - timedelta(days=age_days)).date().isoformat(),
            "planning_status": planning_status,
        }
    )
    return signal


def watch_opportunity(stage: str = "PLANNING_PENDING", **overrides) -> dict:
    return opportunity(customer_lifecycle_stage=stage, **overrides)


def test_planning_watcher_pending_cadence_is_age_aware_and_deterministic() -> None:
    now = datetime(2026, 10, 1, tzinfo=UTC)
    expected = ((10, 7), (60, 14), (120, 30), (200, 30))
    for age_days, cadence in expected:
        signal = watch_signal(now, age_days=age_days)
        first = evaluate_planning_watch(
            watch_opportunity(), signal, has_planning_identity=True, now=now
        )
        second = evaluate_planning_watch(
            watch_opportunity(), signal, has_planning_identity=True, now=now
        )
        assert first == second
        assert first.eligible is True
        assert first.cadence_days == cadence


def test_planning_watcher_appeals_and_needs_review_are_conservative() -> None:
    now = datetime(2026, 10, 1, tzinfo=UTC)
    appeal = watch_signal(
        now,
        outcome="Refused",
        planning_status="Appeal Lodged: REFUSE",
        age_days=40,
    )
    assert evaluate_planning_watch(
        watch_opportunity("APPEAL_PENDING"), appeal, has_planning_identity=True, now=now
    ).cadence_days == 14
    stale_appeal = watch_signal(
        now,
        outcome="Refused",
        planning_status="Appeal Lodged: REFUSE",
        age_days=200,
    )
    assert evaluate_planning_watch(
        watch_opportunity("APPEAL_PENDING"),
        stale_appeal,
        has_planning_identity=True,
        now=now,
    ).cadence_days == 30
    recent_exception = evaluate_planning_watch(
        watch_opportunity("NEEDS_REVIEW"),
        watch_signal(now, age_days=20),
        has_planning_identity=True,
        now=now,
    )
    assert recent_exception.eligible is True
    assert recent_exception.cadence_days == 14


def test_planning_watcher_excludes_unknown_stale_terminal_and_unsafe_cases() -> None:
    now = datetime(2026, 10, 1, tzinfo=UTC)
    unknown = watch_signal(now, outcome="", planning_status="Unknown", age_days=20)
    assert evaluate_planning_watch(
        watch_opportunity(), unknown, has_planning_identity=True, now=now
    ).reason == "unknown_status_without_unresolved_evidence"
    unresolved = watch_signal(now, outcome="", planning_status="In progress", age_days=20)
    admitted = evaluate_planning_watch(
        watch_opportunity(), unresolved, has_planning_identity=True, now=now
    )
    assert admitted.eligible is True
    assert admitted.cadence_days == 7
    stale = watch_signal(now, age_days=731)
    assert evaluate_planning_watch(
        watch_opportunity(), stale, has_planning_identity=True, now=now
    ).reason == "stale_application"
    approved = watch_signal(now, outcome="Approved")
    assert evaluate_planning_watch(
        watch_opportunity("PLANNING_APPROVED"),
        approved,
        has_planning_identity=True,
        now=now,
    ).reason == "planning_already_decided"
    assert evaluate_planning_watch(
        watch_opportunity("STOPPED"),
        watch_signal(now),
        has_planning_identity=True,
        now=now,
    ).reason == "lifecycle_terminal"
    assert evaluate_planning_watch(
        watch_opportunity(publication_automation_blocked=True),
        watch_signal(now),
        has_planning_identity=True,
        now=now,
    ).reason == "manual_automation_block"
    assert evaluate_planning_watch(
        watch_opportunity(), watch_signal(now), has_planning_identity=False, now=now
    ).reason == "insufficient_planning_identity"
    support_only = watch_signal(now, decision="SUPPORT_EXISTING_ONLY")
    assert evaluate_planning_watch(
        watch_opportunity(), support_only, has_planning_identity=True, now=now
    ).reason == "no_relevant_planning_evidence"


def test_planning_watcher_projection_is_bounded_and_side_effect_free() -> None:
    daily, monthly = project_planning_watch_requests(
        {"7_days": 7, "14_days": 14, "30_days": 30}
    )
    assert daily == 3.0
    assert monthly == 90.0


def test_initial_watch_stagger_is_stable_and_spans_each_cadence_window() -> None:
    activated = datetime(2026, 10, 1, 9, 37, tzinfo=UTC)
    for cadence in (7, 14, 30):
        first = deterministic_initial_poll_at("opportunity:signal", cadence, activated)
        second = deterministic_initial_poll_at("opportunity:signal", cadence, activated)
        assert first == second
        assert activated + timedelta(minutes=20) < first
        assert first < activated + timedelta(days=cadence)
    identities = [f"watch-{index}" for index in range(50)]
    hours = {
        deterministic_initial_poll_at(identity, 7, activated).hour for identity in identities
    }
    assert len(hours) > 8


def test_planning_lifecycle_transitions_are_deterministic() -> None:
    assert derive_care_lifecycle([planning("Pending")]).lifecycle == CareLifecycle.PLANNING_PENDING
    assert derive_care_lifecycle([planning("Granted")]).lifecycle == CareLifecycle.PLANNING_APPROVED
    assert derive_care_lifecycle([planning("Refused")]).lifecycle == CareLifecycle.STOPPED
    assert derive_care_lifecycle([planning("Withdrawn")]).lifecycle == CareLifecycle.STOPPED
    assert (
        derive_care_lifecycle(
            [
                planning("Refused", signal_id="refused"),
                planning("Appeal Lodged: REFUSE", signal_id="appeal"),
            ]
        ).lifecycle
        == CareLifecycle.APPEAL_PENDING
    )
    assert (
        derive_care_lifecycle([planning("Appeal Allowed")]).lifecycle
        == CareLifecycle.PLANNING_APPROVED
    )
    assert derive_care_lifecycle([planning("Appeal Dismissed")]).lifecycle == CareLifecycle.STOPPED


def test_stronger_evidence_prevents_planning_downgrade() -> None:
    recruitment = {
        "id": "job-1",
        "source_type": "recruitment",
        "relationship_status": "ACTIVE",
        "review_status": "APPROVED",
        "extracted_facts": {
            "recruitment_relevance": "RELEVANT_CHANGE",
            "commercial_change_evidence": "STRONG",
        },
    }
    routine = {
        **recruitment,
        "id": "job-2",
        "extracted_facts": {
            "recruitment_relevance": "RELEVANT_ROUTINE",
            "commercial_change_evidence": "NONE",
        },
    }
    registered = {
        "id": "ofsted-1",
        "source_type": "ofsted",
        "relationship_status": "ACTIVE",
        "review_status": "APPROVED",
        "metadata": {"registration_status": "Active"},
    }
    assert (
        derive_care_lifecycle([planning("Refused"), recruitment]).lifecycle
        == CareLifecycle.DELIVERY_SIGNAL_DETECTED
    )
    assert (
        derive_care_lifecycle([planning("Pending"), routine]).lifecycle
        == CareLifecycle.PLANNING_PENDING
    )
    assert (
        derive_care_lifecycle([planning("Refused"), registered]).lifecycle
        == CareLifecycle.REGISTERED
    )


def test_support_only_and_rejected_signals_do_not_create_lifecycle() -> None:
    support = planning("Approved", decision="SUPPORT_EXISTING_ONLY", subtype="CONDITION_DISCHARGE")
    rejected = planning("Approved", review_status="REJECTED")
    assert derive_care_lifecycle([support]).lifecycle == CareLifecycle.NEEDS_REVIEW
    assert derive_care_lifecycle([rejected]).lifecycle == CareLifecycle.NEEDS_REVIEW
    assert (
        derive_care_lifecycle([planning("Refused", review_status="REJECTED")]).lifecycle
        == CareLifecycle.STOPPED
    )
    rejected_capacity = planning(
        "Approved",
        review_status="REJECTED",
        subtype="EXPANSION_OR_CAPACITY_CHANGE",
    )
    assert derive_care_lifecycle([rejected_capacity]).lifecycle == CareLifecycle.NEEDS_REVIEW


def test_publication_v2_is_reproducible_safe_stable_and_idempotent() -> None:
    lifecycle = derive_care_lifecycle([planning("Pending")])
    item = opportunity()
    first = evaluate_publication_v2(
        item,
        lifecycle,
        [planning("Pending")],
        hygiene_category="VALID_SUPPORTED",
        hygiene_warning=None,
        safe_title="New children's home — Nottingham, NG8",
        safe_summary="A planning application has been submitted for a children's home.",
    )
    assert CARE_PUBLICATION_POLICY_VERSION == "care-publication-v3"
    assert CARE_PUBLICATION_HOLDOUT_VERSION == "care-opportunity-publication-v1"
    assert first.outcome in {"AUTO_PUBLISH_ELIGIBLE", "QA_HOLDOUT"}
    assert publication_qa_holdout(item["id"]) == publication_qa_holdout(item["id"])
    assert (
        evaluate_publication_v2(
            item,
            lifecycle,
            [planning("Pending")],
            hygiene_category="VALID_SUPPORTED",
            hygiene_warning=None,
            safe_title="New children's home — Nottingham, NG8",
            safe_summary="A planning application has been submitted for a children's home.",
        )
        == first
    )

    assert (
        evaluate_publication_v2(
            item,
            lifecycle,
            [planning("Pending")],
            hygiene_category="UNSUPPORTED_ORPHAN_CANDIDATE",
            hygiene_warning=None,
            safe_title="Safe",
            safe_summary="Safe",
        ).outcome
        == "MANUAL_REVIEW"
    )
    assert (
        "privacy_or_redaction_issue"
        in evaluate_publication_v2(
            item,
            lifecycle,
            [planning("Pending")],
            hygiene_category="VALID_SUPPORTED",
            hygiene_warning=None,
            safe_title="New home — NG8 1LD",
            safe_summary="Safe",
        ).exclusions
    )
    assert (
        "manual_automation_block"
        in evaluate_publication_v2(
            opportunity(publication_automation_blocked=True),
            lifecycle,
            [planning("Pending")],
            hygiene_category="VALID_SUPPORTED",
            hygiene_warning=None,
            safe_title="Safe",
            safe_summary="Safe",
        ).exclusions
    )
    existing = planning("Approved", subtype="LAWFULNESS_EXISTING", decision="SUPPORT_EXISTING_ONLY")
    assert (
        "insufficient_evidence"
        in evaluate_publication_v2(
            item,
            derive_care_lifecycle([existing]),
            [existing],
            hygiene_category="VALID_SUPPORTED",
            hygiene_warning=None,
            safe_title="Safe",
            safe_summary="Safe",
        ).exclusions
    )


def legacy_strong_opening(outcome: str = "Pending") -> dict:
    signal = planning(outcome, subtype="")
    signal["extracted_facts"].update(
        {
            "children_home_relevance": "RELEVANT_CHANGE",
            "commercial_change_evidence": "STRONG",
            "opportunity_change_type": "OPENING",
            "likely_false_positive": False,
            "planning_ambiguity_markers": [],
        }
    )
    return signal


def publication_kwargs() -> dict:
    return {
        "hygiene_category": "VALID_SUPPORTED",
        "hygiene_warning": None,
        "safe_title": "New children's home — Nottingham, NG8",
        "safe_summary": "A planning application has been submitted for a children's home.",
    }


def test_publication_v3_narrowly_admits_strong_pre_taxonomy_openings() -> None:
    signal = legacy_strong_opening()
    v2 = evaluate_publication_v2(
        opportunity(), "PLANNING_PENDING", [signal], **publication_kwargs()
    )
    v3 = evaluate_publication(
        opportunity(), "PLANNING_PENDING", [signal], **publication_kwargs()
    )
    assert v2.outcome == "MANUAL_REVIEW"
    assert "insufficient_evidence" in v2.exclusions
    assert v3.outcome in {"AUTO_PUBLISH_ELIGIBLE", "QA_HOLDOUT"}
    assert "legacy_strong_opening_compatibility" in v3.exclusions

    repeated = evaluate_publication(
        opportunity(), "PLANNING_PENDING", [signal], **publication_kwargs()
    )
    assert repeated == v3
    assert (v3.outcome == "QA_HOLDOUT") == publication_qa_holdout(opportunity()["id"])


def test_publication_v3_does_not_relax_expansion_or_uncertain_lifecycles() -> None:
    signal = legacy_strong_opening("Approved")
    expansion = opportunity(change_type="EXPANSION")
    signal["extracted_facts"]["opportunity_change_type"] = "EXPANSION"
    assert (
        evaluate_publication(
            expansion, "PLANNING_APPROVED", [signal], **publication_kwargs()
        ).outcome
        == "MANUAL_REVIEW"
    )

    opening = legacy_strong_opening("Approved")
    assert (
        evaluate_publication(
            opportunity(), "NEEDS_REVIEW", [opening], **publication_kwargs()
        ).outcome
        == "MANUAL_REVIEW"
    )
    assert (
        evaluate_publication(
            opportunity(), "APPEAL_PENDING", [opening], **publication_kwargs()
        ).outcome
        == "MANUAL_REVIEW"
    )


def test_publication_v3_preserves_manual_and_duplicate_protections() -> None:
    signal = legacy_strong_opening("Approved")
    manual = opportunity(
        publication_status="PUBLISHED",
        customer_published_by="admin@example.com",
        publication_automation_provenance={},
    )
    assert (
        evaluate_publication(manual, "PLANNING_APPROVED", [signal], **publication_kwargs()).outcome
        == "MANUAL_PROTECTION"
    )
    duplicate = opportunity(merged_into_opportunity_id="canonical")
    assert (
        evaluate_publication(
            duplicate, "PLANNING_APPROVED", [signal], **publication_kwargs()
        ).outcome
        == "INELIGIBLE"
    )


def test_publication_conflict_taxonomy_and_sampling_are_deterministic() -> None:
    signal = legacy_strong_opening()
    v2 = evaluate_publication_v2(
        opportunity(), "PLANNING_PENDING", [signal], **publication_kwargs()
    )
    conflict = classify_publication_conflict(
        opportunity(),
        "PLANNING_PENDING",
        [signal],
        hygiene_category="VALID_SUPPORTED",
        v2_decision=v2,
    )
    assert conflict.primary_reason == "pre_taxonomy_strong_opening_excluded"
    assert conflict.judgement == "LIKELY_POLICY_DEFECT"
    assert conflict.customer_useful is True

    expansion = classify_publication_conflict(
        opportunity(change_type="EXPANSION"),
        "PLANNING_APPROVED",
        [planning("Approved", subtype="EXPANSION_OR_CAPACITY_CHANGE")],
        hygiene_category="VALID_SUPPORTED",
        v2_decision=v2,
    )
    assert expansion.primary_reason == "expansion_subtype_not_admitted"
    assert expansion.recommendation == "KEEP_MANUAL_PROTECTION"

    items = [{"opportunity_id": value} for value in ("c", "a", "b")]
    assert deterministic_policy_samples(items, limit=2) == [
        {"opportunity_id": "a"},
        {"opportunity_id": "b"},
    ]


def test_publication_v2_lifecycle_and_manual_protection_outcomes() -> None:
    signal = planning("Approved")
    kwargs = {
        "signals": [signal],
        "hygiene_category": "VALID_SUPPORTED",
        "hygiene_warning": None,
        "safe_title": "New children's home — Nottingham, NG8",
        "safe_summary": "Planning permission has been approved for a children's home.",
    }
    for lifecycle in ("PLANNING_APPROVED", "PLANNING_PENDING"):
        result = evaluate_publication(opportunity(), lifecycle, **kwargs)
        assert result.outcome in {"AUTO_PUBLISH_ELIGIBLE", "QA_HOLDOUT"}
    assert (
        evaluate_publication(opportunity(), "APPEAL_PENDING", **kwargs).outcome
        == "MANUAL_REVIEW"
    )
    assert (
        evaluate_publication(opportunity(), "NEEDS_REVIEW", **kwargs).outcome
        == "MANUAL_REVIEW"
    )
    assert evaluate_publication(opportunity(), "STOPPED", **kwargs).outcome == "INELIGIBLE"
    assert (
        evaluate_publication(
            opportunity(merged_into_opportunity_id="other"),
            "PLANNING_APPROVED",
            **kwargs,
        ).outcome
        == "INELIGIBLE"
    )
    manual = opportunity(
        publication_status="PUBLISHED",
        customer_published_by="admin@example.com",
        publication_automation_provenance={},
    )
    assert (
        evaluate_publication(manual, "PLANNING_APPROVED", **kwargs).outcome
        == "MANUAL_PROTECTION"
    )


def test_publication_v2_requires_strong_evidence_identity_and_safe_content() -> None:
    signal = planning("Pending")
    common = {
        "hygiene_category": "VALID_SUPPORTED",
        "hygiene_warning": None,
        "safe_title": "New children's home — Nottingham, NG8",
        "safe_summary": "A planning application has been submitted for a children's home.",
    }
    weak = planning("Pending", review_status="PENDING")
    decision = evaluate_publication(opportunity(), "PLANNING_PENDING", [weak], **common)
    assert decision.outcome == "MANUAL_REVIEW"
    assert "insufficient_evidence" in decision.exclusions
    identity = opportunity(operator_name=None, address=None, town=None, postcode=None)
    signal["external_id"] = None
    signal["metadata"] = {"decision": "Pending"}
    decision = evaluate_publication(identity, "PLANNING_PENDING", [signal], **common)
    assert "insufficient_identity" in decision.exclusions
    decision = evaluate_publication(
        opportunity(),
        "PLANNING_PENDING",
        [planning("Pending")],
        **{**common, "safe_summary": ""},
    )
    assert "missing_customer_summary" in decision.exclusions


def test_withdrawal_requires_terminal_automatic_publication() -> None:
    refused = planning("Refused")
    stopped = derive_care_lifecycle([refused])
    automated = opportunity(
        publication_status="PUBLISHED",
        customer_published_by="automation",
        publication_automation_provenance={"policy_version": "care-publication-v3"},
    )
    decision = evaluate_withdrawal(automated, stopped, [refused])
    assert decision.outcome == "AUTO_WITHDRAW_ELIGIBLE"
    assert decision.reason == "planning_refused"
    assert CARE_WITHDRAWAL_POLICY_VERSION == "care-withdrawal-v1"

    withdrawn = planning("Withdrawn")
    decision = evaluate_withdrawal(automated, "STOPPED", [withdrawn])
    assert decision.outcome == "AUTO_WITHDRAW_ELIGIBLE"
    assert decision.reason == "planning_withdrawn"

    dismissed = planning("Appeal dismissed")
    decision = evaluate_withdrawal(automated, "STOPPED", [dismissed])
    assert decision.outcome == "AUTO_WITHDRAW_ELIGIBLE"
    assert decision.reason == "appeal_unsuccessful"

    blocked = {
        **automated,
        "publication_automation_blocked": True,
    }
    assert evaluate_withdrawal(blocked, stopped, [refused]).outcome == "MANUAL_PROTECTION"
    manual = opportunity(
        publication_status="PUBLISHED",
        customer_published_by="admin@example.com",
        publication_automation_provenance={},
    )
    assert evaluate_withdrawal(manual, stopped, [refused]).outcome == "MANUAL_PROTECTION"


def test_withdrawal_ambiguous_states_are_conservative_and_deterministic() -> None:
    automated = opportunity(
        publication_status="PUBLISHED",
        publication_automation_provenance={"policy_version": "care-publication-v3"},
    )
    assert evaluate_withdrawal(automated, "NEEDS_REVIEW").outcome == "MANUAL_REVIEW"
    assert evaluate_withdrawal(automated, "APPEAL_PENDING").outcome == "MANUAL_REVIEW"
    first = evaluate_withdrawal(
        automated,
        "PLANNING_PENDING",
        [planning("Pending")],
        hygiene_category="VALID_SUPPORTED",
    )
    second = evaluate_withdrawal(
        automated,
        "PLANNING_PENDING",
        [planning("Pending")],
        hygiene_category="VALID_SUPPORTED",
    )
    assert first == second
    assert first.outcome == "KEEP_PUBLISHED"
    assert evaluate_withdrawal(
        automated,
        "PLANNING_PENDING",
        [planning("Pending")],
        hygiene_category="NEEDS_INVESTIGATION",
    ).outcome == "MANUAL_REVIEW"


def test_withdrawal_handles_merged_and_superseded_automatic_publications() -> None:
    automated = opportunity(
        publication_status="PUBLISHED",
        publication_automation_provenance={"policy_version": "care-publication-v3"},
    )
    assert evaluate_withdrawal(
        {**automated, "merged_into_opportunity_id": "canonical"},
        "PLANNING_APPROVED",
    ).reason == "merged"
    superseded = evaluate_withdrawal(
        automated,
        "PLANNING_APPROVED",
        hygiene_category="SUPERSEDED_CANDIDATE",
    )
    assert superseded.outcome == "AUTO_WITHDRAW_ELIGIBLE"
    assert superseded.reason == "superseded"


def test_holdout_is_approximately_ten_percent() -> None:
    ids = [str(UUID(int=value)) for value in range(1, 1001)]
    held = sum(publication_qa_holdout(value) for value in ids)
    assert 70 <= held <= 130


def test_lifecycle_bootstrap_is_idempotent_and_preserves_versioned_history() -> None:
    records = [
        {"id": "one", "customer_lifecycle_stage": None, "signals": [planning("Pending")]},
        {"id": "two", "customer_lifecycle_stage": None, "signals": [planning("Granted")]},
    ]
    history = []

    def persist(record, decision):
        if record["customer_lifecycle_stage"]:
            return False
        record["customer_lifecycle_stage"] = decision.lifecycle.value
        history.append(
            {
                "id": record["id"],
                "lifecycle": decision.lifecycle.value,
                "policy_version": "care-opportunity-lifecycle-v1",
                "actor_type": "BOOTSTRAP",
            }
        )
        return True

    first = bootstrap_lifecycle_records(records, preview=False, persist=persist)
    assert first["persisted"] == 2
    assert first["history_rows_created"] == 2
    assert first["counts_by_persisted_lifecycle"] == {
        "PLANNING_APPROVED": 1,
        "PLANNING_PENDING": 1,
    }
    second = bootstrap_lifecycle_records(records, preview=False, persist=persist)
    assert second["persisted"] == 0
    assert second["already_populated_skipped"] == 2
    assert len(history) == 2
    assert {item["policy_version"] for item in history} == {"care-opportunity-lifecycle-v1"}
    assert {item["actor_type"] for item in history} == {"BOOTSTRAP"}


def test_lifecycle_bootstrap_continues_after_partial_batch_and_isolates_errors() -> None:
    records = [
        {"id": "one", "customer_lifecycle_stage": None, "signals": [planning("Pending")]},
        {"id": "bad", "customer_lifecycle_stage": None, "signals": [planning("Granted")]},
        {"id": "three", "customer_lifecycle_stage": None, "signals": [planning("Granted")]},
    ]
    provider_calls = 0
    publication_changes = 0
    withdrawal_changes = 0

    def persist(record, decision):
        if record["id"] == "bad":
            raise RuntimeError("isolated write failure")
        record["customer_lifecycle_stage"] = decision.lifecycle.value
        return True

    partial = bootstrap_lifecycle_records(records[:1], preview=False, persist=persist)
    continued = bootstrap_lifecycle_records(records[1:], preview=False, persist=persist)
    assert partial["persisted"] == 1
    assert continued["persisted"] == 1
    assert continued["failed"] == 1
    assert continued["failures"] == [{"opportunity_id": "bad", "error": "RuntimeError"}]
    assert records[2]["customer_lifecycle_stage"] == "PLANNING_APPROVED"
    assert provider_calls == publication_changes == withdrawal_changes == 0


def test_lifecycle_bootstrap_preview_performs_no_writes() -> None:
    calls = 0

    def persist(record, decision):
        nonlocal calls
        calls += 1
        return True

    result = bootstrap_lifecycle_records(
        [{"id": "one", "customer_lifecycle_stage": None, "signals": [planning("Pending")]}],
        preview=True,
        persist=persist,
    )
    assert result["examined"] == 1
    assert result["would_persist"] == 1
    assert result["persisted"] == 0
    assert calls == 0

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any

from app.evidence_support import EvidenceSupport, classify_evidence_support
from app.planning_outcomes import (
    PlanningOutcome,
    canonical_planning_outcome,
    normalize_structured_planning_value,
)

CARE_LIFECYCLE_POLICY_VERSION = "care-opportunity-lifecycle-v1"
CARE_PUBLICATION_POLICY_VERSION = "care-publication-v3"
CARE_PUBLICATION_PREVIOUS_POLICY_VERSION = "care-publication-v2"
CARE_PUBLICATION_LEGACY_POLICY_VERSION = "care-opportunity-publication-v1"
# Holdout identity deliberately remains independent of the policy revision so an
# opportunity does not drift between QA and automatic cohorts after recalculation.
CARE_PUBLICATION_HOLDOUT_VERSION = "care-opportunity-publication-v1"
CARE_WITHDRAWAL_POLICY_VERSION = "care-opportunity-withdrawal-v1"
CARE_PLANNING_WATCHER_POLICY_VERSION = "care-planning-watcher-v2"
CARE_PLANNING_WATCHER_PREVIOUS_MONTHLY_REQUESTS = 3651

WATCHABLE_LIFECYCLES = {
    "PLANNING_PENDING",
    "PLANNING_APPROVED",
    "APPEAL_PENDING",
    "NEEDS_REVIEW",
}
TERMINAL_PLANNING_OUTCOMES = {
    PlanningOutcome.APPROVED,
    PlanningOutcome.REFUSED,
    PlanningOutcome.WITHDRAWN,
    PlanningOutcome.APPEAL_ALLOWED,
    PlanningOutcome.APPEAL_DISMISSED,
}
_EXPLICIT_UNRESOLVED_STATUSES = {
    "IN PROGRESS",
    "AWAITING COMMITTEE",
    "AWAITING DETERMINATION",
    "CONSULTATION",
    "CONSULTATION IN PROGRESS",
    "UNDER ASSESSMENT",
}


class CareLifecycle(StrEnum):
    PLANNING_PENDING = "PLANNING_PENDING"
    PLANNING_APPROVED = "PLANNING_APPROVED"
    DELIVERY_SIGNAL_DETECTED = "DELIVERY_SIGNAL_DETECTED"
    REGISTRATION_DETECTED = "REGISTRATION_DETECTED"
    REGISTERED = "REGISTERED"
    APPEAL_PENDING = "APPEAL_PENDING"
    STOPPED = "STOPPED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


@dataclass(frozen=True)
class LifecycleDecision:
    lifecycle: CareLifecycle
    reason: str
    triggering_signal_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class AutomationDecision:
    outcome: str
    reason: str
    exclusions: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanningWatchDecision:
    eligible: bool
    reason: str
    cadence_days: int | None
    planning_outcome: str
    age_days: int | None
    age_source: str | None


@dataclass(frozen=True)
class PublicationConflictAssessment:
    primary_reason: str
    judgement: str
    customer_useful: bool
    recommendation: str
    planning_subtypes: tuple[str, ...]
    evidence_summary: str


def _watch_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time(), UTC)
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def planning_watch_activity_date(signal: dict[str, Any]) -> tuple[datetime | None, str | None]:
    """Choose the best existing Planning activity timestamp without provider access."""
    metadata = signal.get("metadata") if isinstance(signal.get("metadata"), dict) else {}
    provider_value = metadata.get("provider_record")
    provider = provider_value if isinstance(provider_value, dict) else {}
    candidates = (
        (signal.get("latest_revision_at"), "latest_revision"),
        (metadata.get("status_changed_at"), "status_change"),
        (metadata.get("last_updated"), "status_update"),
        (metadata.get("updated_at"), "status_update"),
        (provider.get("last_updated"), "provider_status_update"),
        (provider.get("updated_at"), "provider_status_update"),
        (provider.get("date_updated"), "provider_status_update"),
        (metadata.get("decision_date"), "decision_date"),
        (provider.get("date_decided"), "decision_date"),
        (metadata.get("application_date"), "application_date"),
        (provider.get("date_received"), "application_date"),
        (signal.get("discovered_at"), "discovered_at"),
    )
    for value, source in candidates:
        if parsed := _watch_datetime(value):
            return parsed, source
    return None, None


def _strong_watch_evidence(signal: dict[str, Any]) -> bool:
    facts = signal.get("extracted_facts") or {}
    return (
        signal.get("review_status") == "APPROVED"
        and facts.get("opportunity_creation_decision") == "CREATE_OPPORTUNITY"
        and classify_evidence_support(signal) == EvidenceSupport.FOUNDATIONAL
    )


def _explicit_unresolved_status(metadata: dict[str, Any]) -> bool:
    return any(
        normalize_structured_planning_value(metadata.get(field)) in _EXPLICIT_UNRESOLVED_STATUSES
        for field in ("planning_status", "status")
    )


def evaluate_planning_watch(
    opportunity: dict[str, Any],
    signal: dict[str, Any],
    *,
    has_planning_identity: bool,
    now: datetime | None = None,
) -> PlanningWatchDecision:
    """Evaluate one existing Planning application for Phase-B2 preview enrollment."""
    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    metadata = signal.get("metadata") if isinstance(signal.get("metadata"), dict) else {}
    outcome = canonical_planning_outcome(metadata).outcome
    activity_at, age_source = planning_watch_activity_date(signal)
    age_days = max((current_time - activity_at).days, 0) if activity_at else None
    lifecycle = str(opportunity.get("customer_lifecycle_stage") or "")

    def excluded(reason: str) -> PlanningWatchDecision:
        return PlanningWatchDecision(False, reason, None, outcome.value, age_days, age_source)

    if opportunity.get("publication_automation_blocked"):
        return excluded("manual_automation_block")
    if lifecycle == CareLifecycle.STOPPED.value:
        return excluded("lifecycle_terminal")
    if lifecycle not in WATCHABLE_LIFECYCLES:
        return excluded("lifecycle_not_watchable")
    if signal.get("source_type") != "planning" or signal.get(
        "relationship_status", signal.get("status")
    ) != "ACTIVE":
        return excluded("no_relevant_planning_evidence")
    if outcome in TERMINAL_PLANNING_OUTCOMES:
        return excluded("planning_already_decided")
    if not _strong_watch_evidence(signal) and outcome != PlanningOutcome.REFUSED_UNDER_APPEAL:
        return excluded("no_relevant_planning_evidence")
    if not has_planning_identity:
        return excluded("insufficient_planning_identity")
    if outcome == PlanningOutcome.UNKNOWN:
        if age_days is not None and age_days > 180:
            return excluded("stale_application")
        if not _explicit_unresolved_status(metadata):
            return excluded("unknown_status_without_unresolved_evidence")
    if outcome not in {
        PlanningOutcome.PENDING,
        PlanningOutcome.UNKNOWN,
        PlanningOutcome.REFUSED_UNDER_APPEAL,
    }:
        return excluded("planning_already_decided")
    if outcome == PlanningOutcome.PENDING and age_days is not None and age_days > 730:
        return excluded("stale_application")

    if outcome == PlanningOutcome.REFUSED_UNDER_APPEAL:
        cadence = 30 if age_days is not None and age_days > 180 else 14
        return PlanningWatchDecision(
            True, "eligible_active_appeal", cadence, outcome.value, age_days, age_source
        )
    if lifecycle == CareLifecycle.NEEDS_REVIEW.value:
        cadence = 14 if age_days is not None and age_days <= 30 else 30
        return PlanningWatchDecision(
            True, "eligible_needs_review_exception", cadence, outcome.value, age_days, age_source
        )
    if age_days is None or age_days <= 30:
        cadence = 7
    elif age_days <= 90:
        cadence = 14
    else:
        cadence = 30
    return PlanningWatchDecision(
        True, "eligible_planning_pending", cadence, outcome.value, age_days, age_source
    )


def project_planning_watch_requests(cadence_counts: dict[str, int]) -> tuple[float, float]:
    """Return deterministic daily/30-day request projections for cadence buckets."""
    monthly = round(
        sum(
            count * 30 / int(cadence.removesuffix("_days"))
            for cadence, count in cadence_counts.items()
        ),
        1,
    )
    return round(monthly / 30, 2), monthly


def deterministic_initial_poll_at(
    watch_identity: str, cadence_days: int, activated_at: datetime
) -> datetime:
    """Spread initial polls stably through the full cadence window and day."""
    if cadence_days not in {7, 14, 30}:
        raise ValueError("unsupported Planning watch cadence")
    anchor = activated_at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    window_seconds = cadence_days * 86400
    digest = hashlib.sha256(
        f"{CARE_PLANNING_WATCHER_POLICY_VERSION}:{watch_identity}".encode()
    ).digest()
    # Keep at least one hour between enrollment and the first provider request.
    offset_seconds = 3600 + int.from_bytes(digest[:8], "big") % (window_seconds - 3600)
    return anchor + timedelta(seconds=offset_seconds)


def planning_watch_snapshot(metadata: dict[str, Any]) -> dict[str, Any]:
    """Small material-state projection used to avoid duplicate Planning revisions."""
    provider_value = metadata.get("provider_record")
    provider = provider_value if isinstance(provider_value, dict) else {}
    decision_value = provider.get("decision")
    provider_decision = decision_value if isinstance(decision_value, dict) else {}
    outcome = canonical_planning_outcome(metadata)
    return {
        "canonical_outcome": outcome.outcome.value,
        "planning_status": normalize_structured_planning_value(
            metadata.get("planning_status") or metadata.get("status") or provider.get("status")
        ),
        "decision": normalize_structured_planning_value(
            metadata.get("decision") or provider_decision.get("outcome")
        ),
        "decision_date": str(
            metadata.get("decision_date")
            or provider.get("date_decided")
            or provider_decision.get("issued_date")
            or ""
        ),
        "description": str(provider.get("description") or metadata.get("description") or "")
        .strip()
        .casefold(),
        "address": str(provider.get("address") or metadata.get("address") or "")
        .strip()
        .casefold(),
        "postcode": str(metadata.get("postcode") or "").replace(" ", "").upper(),
    }


def bootstrap_lifecycle_records(
    opportunities: list[dict[str, Any]],
    *,
    preview: bool,
    persist: Callable[[dict[str, Any], LifecycleDecision], bool],
) -> dict[str, Any]:
    """Derive and persist a bounded record set without coupling to providers."""
    counts: Counter[str] = Counter()
    persisted_counts: Counter[str] = Counter()
    result: dict[str, Any] = {
        "examined": 0,
        "would_persist": 0,
        "persisted": 0,
        "already_populated_skipped": 0,
        "unchanged_skipped": 0,
        "failed": 0,
        "history_rows_created": 0,
        "failures": [],
    }
    for opportunity in opportunities:
        result["examined"] += 1
        if opportunity.get("customer_lifecycle_stage"):
            result["already_populated_skipped"] += 1
            continue
        try:
            decision = derive_care_lifecycle(opportunity.get("signals") or [])
            counts[decision.lifecycle.value] += 1
            result["would_persist"] += 1
            if preview:
                continue
            if not persist(opportunity, decision):
                result["unchanged_skipped"] += 1
                continue
            result["persisted"] += 1
            result["history_rows_created"] += 1
            persisted_counts[decision.lifecycle.value] += 1
        except Exception as error:  # per-record isolation is part of the bootstrap contract
            result["failed"] += 1
            result["failures"].append(
                {
                    "opportunity_id": str(opportunity.get("id") or ""),
                    "error": type(error).__name__,
                }
            )
    result["counts_by_derived_lifecycle"] = dict(sorted(counts.items()))
    result["counts_by_persisted_lifecycle"] = dict(sorted(persisted_counts.items()))
    return result


def publication_qa_holdout(opportunity_id: str) -> bool:
    digest = hashlib.sha256(
        f"{CARE_PUBLICATION_HOLDOUT_VERSION}:{opportunity_id}".encode()
    ).digest()
    return int.from_bytes(digest[:8], "big") % 10 == 0


def _active_approved(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        signal
        for signal in signals
        if signal.get("relationship_status", signal.get("status")) == "ACTIVE"
        and signal.get("review_status") == "APPROVED"
    ]


def _planning_origin_semantics(signal: dict[str, Any]) -> bool:
    facts = signal.get("extracted_facts") or {}
    family_roles = set(signal.get("planning_family_relationship_types") or [])
    return (
        facts.get("opportunity_creation_decision") == "CREATE_OPPORTUNITY"
        or "PRIMARY_APPLICATION" in family_roles
    )


def derive_care_lifecycle(signals: list[dict[str, Any]]) -> LifecycleDecision:
    approved = _active_approved(signals)
    ofsted = [signal for signal in approved if signal.get("source_type") == "ofsted"]
    registered = [
        signal
        for signal in ofsted
        if str((signal.get("metadata") or {}).get("registration_status") or "").upper()
        in {"ACTIVE", "REGISTERED"}
    ]
    if registered:
        return LifecycleDecision(
            CareLifecycle.REGISTERED,
            "Authoritative Ofsted evidence records an active registration.",
            tuple(str(signal["id"]) for signal in registered),
        )
    if ofsted:
        return LifecycleDecision(
            CareLifecycle.REGISTRATION_DETECTED,
            "Ofsted registration evidence is linked to the opportunity.",
            tuple(str(signal["id"]) for signal in ofsted),
        )

    mobilisation = []
    for signal in approved:
        if signal.get("source_type") != "recruitment":
            continue
        facts = signal.get("extracted_facts") or {}
        if (
            facts.get("recruitment_relevance") == "RELEVANT_CHANGE"
            or facts.get("commercial_change_evidence") == "STRONG"
        ):
            mobilisation.append(signal)
    if mobilisation:
        return LifecycleDecision(
            CareLifecycle.DELIVERY_SIGNAL_DETECTED,
            "Strong change-specific recruitment evidence indicates mobilisation.",
            tuple(str(signal["id"]) for signal in mobilisation),
        )

    active_planning = [
        signal
        for signal in signals
        if signal.get("source_type") == "planning"
        and signal.get("relationship_status", signal.get("status")) == "ACTIVE"
        and _planning_origin_semantics(signal)
    ]
    planning = [signal for signal in approved if signal in active_planning]
    foundational = []
    for signal in planning:
        facts = signal.get("extracted_facts") or {}
        outcome = canonical_planning_outcome(signal.get("metadata") or {}).outcome
        if classify_evidence_support(signal) == EvidenceSupport.FOUNDATIONAL:
            foundational.append(signal)
    outcomes = {
        canonical_planning_outcome(signal.get("metadata") or {}).outcome: []
        for signal in foundational
    }
    for signal in foundational:
        outcome = canonical_planning_outcome(signal.get("metadata") or {}).outcome
        outcomes.setdefault(outcome, []).append(signal)

    positive = outcomes.get(PlanningOutcome.APPROVED, []) + outcomes.get(
        PlanningOutcome.APPEAL_ALLOWED, []
    )
    if positive:
        return LifecycleDecision(
            CareLifecycle.PLANNING_APPROVED,
            "Approved foundational Planning evidence supports the opportunity.",
            tuple(str(signal["id"]) for signal in positive),
        )
    appeals = [
        signal
        for signal in active_planning
        if canonical_planning_outcome(signal.get("metadata") or {}).outcome
        == PlanningOutcome.REFUSED_UNDER_APPEAL
    ]
    if appeals:
        return LifecycleDecision(
            CareLifecycle.APPEAL_PENDING,
            "The foundational Planning application is under active appeal.",
            tuple(str(signal["id"]) for signal in appeals),
        )
    pending = outcomes.get(PlanningOutcome.PENDING, []) + outcomes.get(PlanningOutcome.UNKNOWN, [])
    if pending:
        return LifecycleDecision(
            CareLifecycle.PLANNING_PENDING,
            "A reviewed foundational Planning application is pending or not yet decided.",
            tuple(str(signal["id"]) for signal in pending),
        )

    negative = [
        signal
        for signal in active_planning
        if canonical_planning_outcome(signal.get("metadata") or {}).outcome
        in {
            PlanningOutcome.REFUSED,
            PlanningOutcome.WITHDRAWN,
            PlanningOutcome.APPEAL_DISMISSED,
        }
    ]
    if negative:
        return LifecycleDecision(
            CareLifecycle.STOPPED,
            "Only terminal-negative Planning evidence remains; no stronger active "
            "evidence keeps the opportunity alive.",
            tuple(str(signal["id"]) for signal in negative),
        )
    return LifecycleDecision(
        CareLifecycle.NEEDS_REVIEW,
        "Current reviewed evidence does not determine a customer lifecycle safely.",
        warnings=("NO_DETERMINISTIC_LIFECYCLE",),
    )


ACTIVE_PUBLICATION_LIFECYCLES = {
    CareLifecycle.PLANNING_PENDING,
    CareLifecycle.PLANNING_APPROVED,
    CareLifecycle.DELIVERY_SIGNAL_DETECTED,
    CareLifecycle.REGISTRATION_DETECTED,
    CareLifecycle.REGISTERED,
}


def evaluate_publication_v1(
    opportunity: dict[str, Any],
    lifecycle: LifecycleDecision,
    signals: list[dict[str, Any]],
    *,
    hygiene_category: str,
    hygiene_warning: str | None,
    safe_title: str | None,
    safe_summary: str | None,
) -> AutomationDecision:
    exclusions: list[str] = []
    if opportunity.get("vertical") != "CHILDRENS_HOME":
        exclusions.append("WRONG_VERTICAL")
    if opportunity.get("publication_status") != "DRAFT":
        exclusions.append("NOT_DRAFT")
    if hygiene_category != "VALID_SUPPORTED":
        exclusions.append("HYGIENE")
    if hygiene_warning:
        exclusions.append("WARNING")
    if lifecycle.lifecycle not in ACTIVE_PUBLICATION_LIFECYCLES:
        exclusions.append("LIFECYCLE")
    if opportunity.get("change_type") != "OPENING":
        exclusions.append("INITIAL_POLICY_SCOPE")
    if not lifecycle.triggering_signal_ids:
        exclusions.append("NO_FOUNDATIONAL_EVIDENCE")
    narrow_foundations = []
    for signal in _active_approved(signals):
        if signal.get("source_type") != "planning":
            continue
        facts = signal.get("extracted_facts") or {}
        outcome = canonical_planning_outcome(signal.get("metadata") or {}).outcome
        if (
            classify_evidence_support(signal) == EvidenceSupport.FOUNDATIONAL
            and facts.get("planning_subtype")
            in {
                "NEW_HOME_CHANGE_OF_USE",
                "NEW_HOME_OTHER_EXPLICIT",
                "LAWFULNESS_PROPOSED",
            }
            and outcome
            in {
                PlanningOutcome.PENDING,
                PlanningOutcome.APPROVED,
                PlanningOutcome.APPEAL_ALLOWED,
            }
        ):
            narrow_foundations.append(signal)
    if not narrow_foundations:
        exclusions.append("INITIAL_POLICY_SCOPE")
    if opportunity.get("publication_automation_blocked"):
        exclusions.append("MANUAL_BLOCK")
    if not safe_title or not safe_summary:
        exclusions.append("SAFE_CONTENT")
    location = any(
        opportunity.get(field) for field in ("town", "local_authority", "region", "postcode")
    )
    if not location:
        exclusions.append("SAFE_GEOGRAPHY")
    combined = f"{safe_title or ''} {safe_summary or ''}".upper()
    full_postcode = str(opportunity.get("postcode") or "").strip().upper()
    address = str(opportunity.get("address") or "").strip().upper()
    if full_postcode and full_postcode in combined:
        exclusions.append("FULL_POSTCODE_LEAK")
    if address and len(address) >= 6 and address in combined:
        exclusions.append("ADDRESS_LEAK")
    if exclusions:
        return AutomationDecision("NOT_ELIGIBLE", exclusions[0], tuple(exclusions))
    if publication_qa_holdout(str(opportunity["id"])):
        return AutomationDecision(
            "QA_HOLDOUT",
            "Stable 10% publication QA holdout.",
        )
    return AutomationDecision(
        "AUTO_PUBLISH",
        "Supported opportunity passed deterministic publication safeguards.",
    )


_PUBLICATION_FOUNDATION_SUBTYPES = {
    "NEW_HOME_CHANGE_OF_USE",
    "NEW_HOME_OTHER_EXPLICIT",
    "LAWFULNESS_PROPOSED",
}
_PUBLICATION_POSITIVE_OUTCOMES = {
    PlanningOutcome.PENDING,
    PlanningOutcome.APPROVED,
    PlanningOutcome.APPEAL_ALLOWED,
}
_PUBLICATION_NEGATIVE_OUTCOMES = {
    PlanningOutcome.REFUSED,
    PlanningOutcome.WITHDRAWN,
    PlanningOutcome.REFUSED_UNDER_APPEAL,
    PlanningOutcome.APPEAL_DISMISSED,
}


def _publication_foundations(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    foundations: list[dict[str, Any]] = []
    for signal in _active_approved(signals):
        if signal.get("source_type") != "planning":
            continue
        facts = signal.get("extracted_facts") or {}
        if (
            classify_evidence_support(signal) == EvidenceSupport.FOUNDATIONAL
            and facts.get("opportunity_creation_decision") == "CREATE_OPPORTUNITY"
            and facts.get("planning_subtype") in _PUBLICATION_FOUNDATION_SUBTYPES
            and canonical_planning_outcome(signal.get("metadata") or {}).outcome
            in _PUBLICATION_POSITIVE_OUTCOMES
        ):
            foundations.append(signal)
    return foundations


def _has_publication_identity(
    opportunity: dict[str, Any], signals: list[dict[str, Any]]
) -> bool:
    has_site = bool(
        opportunity.get("town")
        or opportunity.get("postcode")
        or opportunity.get("address")
        or opportunity.get("local_authority")
    )
    has_organisation_or_application = bool(opportunity.get("operator_name")) or any(
        signal.get("source_type") == "planning"
        and signal.get("external_id")
        and (
            (signal.get("metadata") or {}).get("local_authority")
            or (signal.get("metadata") or {}).get("council")
            or (signal.get("metadata") or {}).get("planning_authority")
        )
        for signal in signals
    )
    return has_site and has_organisation_or_application


def evaluate_publication_v2(
    opportunity: dict[str, Any],
    lifecycle: str | LifecycleDecision,
    signals: list[dict[str, Any]],
    *,
    hygiene_category: str,
    hygiene_warning: str | None,
    safe_title: str | None,
    safe_summary: str | None,
    respect_existing_publication: bool = True,
) -> AutomationDecision:
    """Evaluate preview-only Care publication v2 from persisted current state."""
    lifecycle_value = (
        lifecycle.lifecycle.value if isinstance(lifecycle, LifecycleDecision) else str(lifecycle)
    )
    provenance = opportunity.get("publication_automation_provenance") or {}
    manually_published = bool(opportunity.get("customer_published_by")) and not provenance.get(
        "policy_version"
    )
    if respect_existing_publication and opportunity.get("publication_status") == "PUBLISHED":
        if manually_published:
            return AutomationDecision(
                "MANUAL_PROTECTION",
                "Existing manual publication is authoritative.",
                ("manual_publication_protection",),
            )
        return AutomationDecision(
            "ALREADY_PUBLISHED",
            "Opportunity is already published.",
            ("already_published",),
        )
    if opportunity.get("publication_automation_blocked"):
        return AutomationDecision(
            "MANUAL_PROTECTION",
            "Publication automation is manually blocked.",
            ("manual_automation_block",),
        )

    hard_exclusions: list[str] = []
    review_reasons: list[str] = []
    if opportunity.get("vertical") != "CHILDRENS_HOME":
        hard_exclusions.append("wrong_vertical")
    if opportunity.get("merged_into_opportunity_id") or opportunity.get("review_status") in {
        "MERGED",
        "REJECTED",
    }:
        hard_exclusions.append("duplicate_or_superseded")
    if hygiene_category in {"DUPLICATE_CANDIDATE", "SUPERSEDED_CANDIDATE"}:
        hard_exclusions.append("duplicate_or_superseded")
    if lifecycle_value == CareLifecycle.STOPPED.value:
        hard_exclusions.append("stopped")
    elif lifecycle_value == CareLifecycle.NEEDS_REVIEW.value:
        review_reasons.append("needs_review_lifecycle")
    elif lifecycle_value == CareLifecycle.APPEAL_PENDING.value:
        review_reasons.append("unresolved_appeal")
    elif lifecycle_value not in {stage.value for stage in ACTIVE_PUBLICATION_LIFECYCLES}:
        review_reasons.append("unsupported_lifecycle")

    active_planning = [
        signal
        for signal in signals
        if signal.get("source_type") == "planning"
        and signal.get("relationship_status", signal.get("status")) == "ACTIVE"
    ]
    if any(
        canonical_planning_outcome(signal.get("metadata") or {}).outcome
        in _PUBLICATION_NEGATIVE_OUTCOMES
        and classify_evidence_support(signal) == EvidenceSupport.FOUNDATIONAL
        for signal in active_planning
    ):
        review_reasons.append("negative_planning_state")

    foundations = _publication_foundations(signals)
    if not foundations:
        review_reasons.append("insufficient_evidence")
    if hygiene_category == "NEEDS_INVESTIGATION" or hygiene_warning:
        review_reasons.append("unresolved_evidence_warning")
    elif hygiene_category == "UNSUPPORTED_ORPHAN_CANDIDATE":
        review_reasons.append("insufficient_evidence")
    elif hygiene_category == "MANUAL_OR_ADMIN_TOUCHED_PRESERVE":
        review_reasons.append("manual_history_review")
    if not _has_publication_identity(opportunity, signals):
        review_reasons.append("insufficient_identity")
    if not safe_title or not safe_summary or len(str(safe_summary).strip()) < 20:
        review_reasons.append("missing_customer_summary")

    combined = f"{safe_title or ''} {safe_summary or ''}".upper()
    full_postcode = str(opportunity.get("postcode") or "").strip().upper()
    address = str(opportunity.get("address") or "").strip().upper()
    if (full_postcode and full_postcode in combined) or (
        address and len(address) >= 6 and address in combined
    ):
        review_reasons.append("privacy_or_redaction_issue")

    if hard_exclusions:
        reasons = tuple(dict.fromkeys(hard_exclusions + review_reasons))
        return AutomationDecision("INELIGIBLE", reasons[0], reasons)
    if review_reasons:
        reasons = tuple(dict.fromkeys(review_reasons))
        return AutomationDecision("MANUAL_REVIEW", reasons[0], reasons)
    if publication_qa_holdout(str(opportunity["id"])):
        return AutomationDecision(
            "QA_HOLDOUT",
            "Stable 10% publication QA holdout.",
            ("stable_qa_holdout",),
        )
    return AutomationDecision(
        "AUTO_PUBLISH_ELIGIBLE",
        "Current lifecycle, foundational evidence, identity and safe content passed policy.",
    )


def _legacy_strong_opening_signal(
    opportunity: dict[str, Any], signal: dict[str, Any]
) -> bool:
    """Recognise safe pre-taxonomy opening facts without reparsing proposal text."""
    if opportunity.get("change_type") != "OPENING":
        return False
    if signal.get("source_type") != "planning" or signal.get("review_status") != "APPROVED":
        return False
    if signal.get("relationship_status", signal.get("status")) != "ACTIVE":
        return False
    facts = signal.get("extracted_facts") or {}
    if facts.get("planning_subtype"):
        return False
    if facts.get("opportunity_creation_decision") != "CREATE_OPPORTUNITY":
        return False
    if facts.get("children_home_relevance") != "RELEVANT_CHANGE":
        return False
    if facts.get("commercial_change_evidence") != "STRONG":
        return False
    if facts.get("opportunity_change_type") != "OPENING":
        return False
    if facts.get("likely_false_positive") is True or facts.get("planning_ambiguity_markers"):
        return False
    if classify_evidence_support(signal) != EvidenceSupport.FOUNDATIONAL:
        return False
    return (
        canonical_planning_outcome(signal.get("metadata") or {}).outcome
        in _PUBLICATION_POSITIVE_OUTCOMES
    )


def legacy_strong_opening_signals(
    opportunity: dict[str, Any], signals: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    return [signal for signal in signals if _legacy_strong_opening_signal(opportunity, signal)]


def deterministic_policy_samples(
    items: list[dict[str, Any]], *, limit: int = 5
) -> list[dict[str, Any]]:
    """Return a reproducible bounded sample without runtime randomness."""
    return sorted(items, key=lambda item: str(item.get("opportunity_id") or item.get("id") or ""))[
        :limit
    ]


def classify_publication_conflict(
    opportunity: dict[str, Any],
    lifecycle: str,
    signals: list[dict[str, Any]],
    *,
    hygiene_category: str,
    v2_decision: AutomationDecision,
) -> PublicationConflictAssessment:
    active_planning = [
        signal
        for signal in signals
        if signal.get("source_type") == "planning"
        and signal.get("relationship_status", signal.get("status")) == "ACTIVE"
    ]
    subtypes = tuple(
        sorted(
            {
                str((signal.get("extracted_facts") or {}).get("planning_subtype") or "UNSET")
                for signal in active_planning
            }
        )
    )
    foundational = sum(
        classify_evidence_support(signal) == EvidenceSupport.FOUNDATIONAL
        for signal in active_planning
    )
    outcomes = sorted(
        {
            canonical_planning_outcome(signal.get("metadata") or {}).outcome.value
            for signal in active_planning
        }
    )
    summary = (
        f"{foundational} foundational Planning signal(s); subtypes "
        f"{', '.join(subtypes) or 'none'}; outcomes {', '.join(outcomes) or 'none'}."
    )
    if opportunity.get("merged_into_opportunity_id") or hygiene_category in {
        "DUPLICATE_CANDIDATE",
        "SUPERSEDED_CANDIDATE",
    }:
        return PublicationConflictAssessment(
            "superseded_or_duplicate",
            "VALID_HISTORICAL_OR_MANUAL_EXCEPTION",
            False,
            "KEEP_MANUAL_PROTECTION",
            subtypes,
            summary,
        )
    if lifecycle == CareLifecycle.APPEAL_PENDING.value:
        return PublicationConflictAssessment(
            "unresolved_appeal",
            "AMBIGUOUS_NEEDS_HUMAN_JUDGEMENT",
            False,
            "KEEP_MANUAL_PROTECTION",
            subtypes,
            summary,
        )
    if lifecycle == CareLifecycle.NEEDS_REVIEW.value:
        return PublicationConflictAssessment(
            "lifecycle_mismatch",
            "AMBIGUOUS_NEEDS_HUMAN_JUDGEMENT",
            foundational > 0,
            "KEEP_MANUAL_PROTECTION",
            subtypes,
            summary,
        )
    if legacy_strong_opening_signals(opportunity, signals):
        return PublicationConflictAssessment(
            "pre_taxonomy_strong_opening_excluded",
            "LIKELY_POLICY_DEFECT",
            True,
            "REFINE_POLICY",
            subtypes,
            summary,
        )
    if "EXPANSION_OR_CAPACITY_CHANGE" in subtypes or opportunity.get("change_type") == "EXPANSION":
        return PublicationConflictAssessment(
            "expansion_subtype_not_admitted",
            "AMBIGUOUS_NEEDS_HUMAN_JUDGEMENT",
            foundational > 0,
            "KEEP_MANUAL_PROTECTION",
            subtypes,
            summary,
        )
    if "manual_history_review" in v2_decision.exclusions:
        return PublicationConflictAssessment(
            "legacy_manual_history_ambiguity",
            "VALID_HISTORICAL_OR_MANUAL_EXCEPTION",
            foundational > 0,
            "KEEP_MANUAL_PROTECTION",
            subtypes,
            summary,
        )
    return PublicationConflictAssessment(
        "insufficient_current_evidence",
        "VALID_HISTORICAL_OR_MANUAL_EXCEPTION",
        foundational > 0,
        "KEEP_MANUAL_PROTECTION",
        subtypes,
        summary,
    )


def evaluate_publication(
    opportunity: dict[str, Any],
    lifecycle: str | LifecycleDecision,
    signals: list[dict[str, Any]],
    *,
    hygiene_category: str,
    hygiene_warning: str | None,
    safe_title: str | None,
    safe_summary: str | None,
    respect_existing_publication: bool = True,
) -> AutomationDecision:
    """Evaluate v3, narrowly admitting strong pre-taxonomy opening evidence."""
    v2 = evaluate_publication_v2(
        opportunity,
        lifecycle,
        signals,
        hygiene_category=hygiene_category,
        hygiene_warning=hygiene_warning,
        safe_title=safe_title,
        safe_summary=safe_summary,
        respect_existing_publication=respect_existing_publication,
    )
    if v2.outcome != "MANUAL_REVIEW" or "insufficient_evidence" not in v2.exclusions:
        return v2
    legacy = legacy_strong_opening_signals(opportunity, signals)
    if not legacy:
        return v2

    projected_signals: list[dict[str, Any]] = []
    legacy_ids = {str(signal.get("id")) for signal in legacy}
    for signal in signals:
        if str(signal.get("id")) not in legacy_ids:
            projected_signals.append(signal)
            continue
        projected_signals.append(
            {
                **signal,
                "extracted_facts": {
                    **(signal.get("extracted_facts") or {}),
                    "planning_subtype": "NEW_HOME_OTHER_EXPLICIT",
                },
            }
        )
    refined = evaluate_publication_v2(
        opportunity,
        lifecycle,
        projected_signals,
        hygiene_category=hygiene_category,
        hygiene_warning=hygiene_warning,
        safe_title=safe_title,
        safe_summary=safe_summary,
        respect_existing_publication=respect_existing_publication,
    )
    if refined.outcome == "AUTO_PUBLISH_ELIGIBLE":
        return AutomationDecision(
            refined.outcome,
            "Strong reviewed pre-taxonomy opening evidence passed the v3 compatibility rule.",
            ("legacy_strong_opening_compatibility",),
        )
    if refined.outcome == "QA_HOLDOUT":
        return AutomationDecision(
            refined.outcome,
            refined.reason,
            ("stable_qa_holdout", "legacy_strong_opening_compatibility"),
        )
    return refined


def evaluate_withdrawal(
    opportunity: dict[str, Any], lifecycle: LifecycleDecision
) -> AutomationDecision:
    if opportunity.get("publication_status") != "PUBLISHED":
        return AutomationDecision("NOT_ELIGIBLE", "NOT_PUBLISHED", ("NOT_PUBLISHED",))
    if lifecycle.lifecycle != CareLifecycle.STOPPED:
        return AutomationDecision("KEEP_PUBLISHED", "ACTIVE_OR_UNRESOLVED_LIFECYCLE")
    if opportunity.get("publication_automation_blocked"):
        return AutomationDecision("MANUAL_REVIEW", "MANUAL_BLOCK", ("MANUAL_BLOCK",))
    provenance = opportunity.get("publication_automation_provenance") or {}
    if opportunity.get("customer_published_by") and not provenance.get("policy_version"):
        return AutomationDecision(
            "MANUAL_REVIEW",
            "MANUAL_PUBLICATION",
            ("MANUAL_PUBLICATION",),
        )
    return AutomationDecision(
        "AUTO_WITHDRAW",
        "Terminal-negative evidence remains without an alternative active foundation.",
    )

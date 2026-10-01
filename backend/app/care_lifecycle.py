from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from app.evidence_support import EvidenceSupport, classify_evidence_support
from app.planning_outcomes import (
    PlanningOutcome,
    canonical_planning_outcome,
    normalize_structured_planning_value,
)

CARE_LIFECYCLE_POLICY_VERSION = "care-opportunity-lifecycle-v1"
CARE_PUBLICATION_POLICY_VERSION = "care-opportunity-publication-v1"
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
    digest = hashlib.sha256(f"{CARE_PUBLICATION_POLICY_VERSION}:{opportunity_id}".encode()).digest()
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


def evaluate_publication(
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

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from statistics import median
from typing import Any

from app.correlation import classify_match, compatible_names, normalize_identity
from app.verticals import policy_for, validate_vertical

BACKTEST_ENGINE_VERSION = "historical-replay-v1"
POSITIVE_OUTCOMES = {"OPENED", "REGISTERED", "EXPANDED", "RELOCATED"}
NEGATIVE_OUTCOMES = {"DID_NOT_OPEN", "ABANDONED"}
_POSTCODE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b", re.IGNORECASE)


@dataclass(frozen=True)
class BacktestBounds:
    as_of: datetime
    lookback_days: int = 365
    max_cases: int = 30
    max_signals: int = 500

    @classmethod
    def from_values(
        cls,
        *,
        as_of: str | datetime,
        lookback_days: int = 365,
        max_cases: int = 30,
        max_signals: int = 500,
    ) -> BacktestBounds:
        parsed = _datetime(as_of)
        if parsed is None:
            raise ValueError("as_of must be an ISO date or timestamp")
        return cls(
            as_of=parsed,
            lookback_days=min(max(int(lookback_days), 180), 365),
            max_cases=min(max(int(max_cases), 1), 50),
            max_signals=min(max(int(max_signals), 1), 1000),
        )


def _datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, datetime.min.time(), tzinfo=UTC)
    else:
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def historical_availability(signal: dict[str, Any]) -> datetime | None:
    """Return when this source item could first have been known.

    Planning and recruitment use their source publication fields. Ofsted annual-register
    rows use retrieval time: a registration date is outcome truth, never publication time.
    Unknown sources use the preserved discovery/retrieval timestamp.
    """
    metadata = signal.get("metadata") or {}
    source_type = str(signal.get("source_type") or "").lower()
    if source_type == "planning":
        for key in ("publication_date", "validated_date", "date_received", "application_date"):
            if parsed := _datetime(metadata.get(key)):
                return parsed
    elif source_type == "recruitment":
        if parsed := _datetime(metadata.get("published_at")):
            return parsed
    elif source_type == "ofsted":
        # Never substitute registration_date here. Current annual snapshots did not
        # necessarily exist on the historic registration date.
        return _datetime(signal.get("persisted_at") or signal.get("discovered_at"))
    return _datetime(signal.get("discovered_at") or signal.get("persisted_at"))


def historical_signal_snapshot(signal: dict[str, Any]) -> dict[str, Any]:
    """Mask mutable present-day fields that lack a dated historical snapshot."""
    value = deepcopy(signal)
    if str(value.get("source_type") or "").lower() != "planning":
        return value
    metadata = value.get("metadata") or {}
    for key in (
        "planning_status",
        "decision",
        "decision_date",
        "last_updated",
        "updated_at",
    ):
        metadata.pop(key, None)
    provider_record = metadata.get("provider_record")
    if isinstance(provider_record, dict):
        for key in (
            "status",
            "planning_status",
            "decision",
            "decision_date",
            "date_decided",
            "last_updated",
            "updated_at",
        ):
            provider_record.pop(key, None)
    value["metadata"] = metadata
    return value


def company_identity_available(evidence: dict[str, Any], as_of: datetime) -> dict[str, Any] | None:
    """Return only temporally safe Companies House identity fields.

    Current status, SIC and registered office are mutable and deliberately excluded.
    A legal name/company number is admitted only after the evidence retrieval time and
    after incorporation.
    """
    retrieved_at = _datetime(evidence.get("retrieved_at"))
    metadata = evidence.get("safe_metadata") or {}
    incorporated_at = _datetime(metadata.get("date_of_creation"))
    if retrieved_at is None or retrieved_at > as_of:
        return None
    if incorporated_at and incorporated_at > as_of:
        return None
    return {
        "operator_id": evidence.get("operator_id"),
        "company_number": metadata.get("company_number"),
        "legal_name": metadata.get("company_name"),
        "incorporation_date": metadata.get("date_of_creation"),
        "available_at": retrieved_at,
    }


def run_fingerprint(*, benchmark_version: str, vertical: str, bounds: BacktestBounds) -> str:
    payload = {
        "engine": BACKTEST_ENGINE_VERSION,
        "benchmark_version": benchmark_version,
        "vertical": validate_vertical(vertical),
        "as_of": bounds.as_of.isoformat(),
        "lookback_days": bounds.lookback_days,
        "max_cases": bounds.max_cases,
        "max_signals": bounds.max_signals,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _postcode(signal: dict[str, Any], candidate: dict[str, Any]) -> str | None:
    metadata = signal.get("metadata") or {}
    direct = metadata.get("postcode") or metadata.get("post_code")
    if direct:
        return str(direct).upper().strip()
    match = _POSTCODE.search(
        " ".join(
            str(value or "")
            for value in (
                signal.get("location_hint"),
                candidate.get("address"),
            )
        )
    )
    return match.group(1).upper().strip() if match else None


def _identity(signal: dict[str, Any], candidate: dict[str, Any]) -> str:
    return str(
        candidate.get("operator_name")
        or signal.get("organisation_hint")
        or candidate.get("nursery_name")
        or signal.get("title")
        or ""
    )


def _location(signal: dict[str, Any]) -> str:
    metadata = signal.get("metadata") or {}
    return str(
        metadata.get("local_authority")
        or metadata.get("council")
        or metadata.get("locality")
        or metadata.get("town")
        or signal.get("location_hint")
        or ""
    )


def _locations_compatible(left: Any, right: Any) -> bool:
    first = set(normalize_identity(left).split())
    second = set(normalize_identity(right).split())
    return bool(first and second and (first <= second or second <= first))


def _matches_truth(opportunity: dict[str, Any], case: dict[str, Any]) -> bool:
    known_postcode = normalize_identity(case.get("known_postcode"))
    opportunity_postcode = normalize_identity(opportunity.get("postcode"))
    if known_postcode and opportunity_postcode and known_postcode != opportunity_postcode:
        return False
    known_name = case.get("known_operator") or case.get("known_site")
    known_location = case.get("known_location")
    if (
        not known_postcode
        and known_location
        and not _locations_compatible(known_location, opportunity.get("location"))
    ):
        return False
    if known_name and opportunity.get("identity"):
        if normalize_identity(known_name) == normalize_identity(opportunity["identity"]):
            return bool(known_postcode or known_location)
        if compatible_names(known_name, opportunity["identity"]):
            return bool((known_postcode and opportunity_postcode) or known_location)
    return bool(known_postcode and opportunity_postcode and known_postcode == opportunity_postcode)


def _signal_relevance_to_truth(
    signal: dict[str, Any], candidate: dict[str, Any], case: dict[str, Any]
) -> bool:
    return _matches_truth(
        {
            "postcode": _postcode(signal, candidate),
            "identity": _identity(signal, candidate),
            "location": _location(signal),
        },
        case,
    )


def _operator_resolution(
    signal: dict[str, Any], candidate: dict[str, Any], case: dict[str, Any]
) -> str:
    metadata = signal.get("metadata") or {}
    known_company = normalize_identity(case.get("known_company_number"))
    observed_company = normalize_identity(
        metadata.get("companies_house_number") or metadata.get("company_number")
    )
    if known_company and known_company == observed_company:
        return "EXACT"
    known_name = case.get("known_operator")
    observed_name = _identity(signal, candidate)
    if known_name and normalize_identity(known_name) == normalize_identity(observed_name):
        return "STRONG"
    if known_name and compatible_names(known_name, observed_name):
        return "PROBABLE"
    return "UNRESOLVED"


def _percentile(values: list[int], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 2)


def replay_case(
    case: dict[str, Any],
    signals: list[dict[str, Any]],
    company_evidence: list[dict[str, Any]],
    bounds: BacktestBounds,
) -> dict[str, Any]:
    """Replay one case without mutating production state or consulting current APIs."""
    vertical = validate_vertical(str(case["vertical"]))
    outcome_at = _datetime(case.get("outcome_date"))
    if outcome_at is None:
        return _excluded_case(case, "known outcome date is missing")
    replay_end = min(outcome_at, bounds.as_of)
    replay_start = outcome_at - timedelta(days=bounds.lookback_days)
    eligible: list[tuple[datetime, dict[str, Any]]] = []
    for signal in signals[: bounds.max_signals]:
        if signal.get("vertical") != vertical:
            continue
        available_at = historical_availability(signal)
        if available_at is None or not (replay_start <= available_at <= replay_end):
            continue
        # For CareSignal's initial benchmark Ofsted defines truth and cannot be an input.
        if vertical == "CHILDRENS_HOME" and signal.get("source_type") == "ofsted":
            continue
        eligible.append((available_at, historical_signal_snapshot(signal)))
    eligible.sort(key=lambda item: (item[0], str(item[1].get("id") or "")))
    if not eligible:
        return _excluded_case(case, "no historically reconstructable planning/recruitment coverage")

    policy = policy_for(vertical)
    opportunities: list[dict[str, Any]] = []
    timeline: list[dict[str, Any]] = []
    review_items = 0
    relevant_sources: list[tuple[datetime, str]] = []
    first_operator_resolution = "UNRESOLVED"
    operator_resolved_at: datetime | None = None

    for available_at, signal in eligible:
        candidate = policy.classify_signal(signal)
        decision = policy.determine_opportunity_action(candidate)
        postcode = _postcode(signal, candidate)
        identity = _identity(signal, candidate)
        confidence = float(candidate.get("confidence") or 0)
        related_to_truth = _signal_relevance_to_truth(signal, candidate, case)
        if related_to_truth and decision.decision != "IGNORE_FOR_OPPORTUNITY":
            relevant_sources.append((available_at, str(signal.get("source_type"))))
            resolution = _operator_resolution(signal, candidate, case)
            if first_operator_resolution == "UNRESOLVED":
                first_operator_resolution = resolution
            if resolution in {"EXACT", "STRONG"} and operator_resolved_at is None:
                operator_resolved_at = available_at

        best: tuple[int, dict[str, Any], Any] | None = None
        rank = {"EXACT": 4, "STRONG": 3, "PROBABLE": 2, "UNCERTAIN": 1, "NO_MATCH": 0}
        for opportunity in opportunities:
            match = classify_match(
                postcode, identity, opportunity["postcode"], opportunity["identity"]
            )
            if best is None or rank[match.outcome] > best[0]:
                best = (rank[match.outcome], opportunity, match)

        action = "ignored"
        match_confidence: float | None = None
        if best and best[2].outcome in {"EXACT", "STRONG"}:
            best[1]["signals"].append(str(signal.get("id")))
            best[1]["latest_at"] = available_at
            best[1]["event_confidence"] = max(best[1]["event_confidence"], confidence)
            match_confidence = float(best[2].confidence)
            action = "supporting signal linked"
        elif decision.decision == "CREATE_OPPORTUNITY":
            opportunities.append(
                {
                    "key": hashlib.sha256(
                        f"{vertical}|{postcode}|{normalize_identity(identity)}|{decision.change_type}".encode()
                    ).hexdigest(),
                    "vertical": vertical,
                    "postcode": postcode,
                    "identity": identity,
                    "location": _location(signal),
                    "change_type": decision.change_type,
                    "created_at": available_at,
                    "latest_at": available_at,
                    "signals": [str(signal.get("id"))],
                    "source_type": signal.get("source_type"),
                    "event_confidence": confidence,
                    "match_confidence": None,
                    "lifecycle_stage": candidate.get("lifecycle_stage"),
                }
            )
            action = "opportunity created"
        elif decision.decision == "REVIEW" or (
            best and best[2].outcome in {"PROBABLE", "UNCERTAIN"}
        ):
            review_items += 1
            match_confidence = float(best[2].confidence) if best else None
            action = "review generated"
        elif decision.decision == "SUPPORT_EXISTING_ONLY":
            action = "supporting evidence unmatched"

        if decision.decision != "IGNORE_FOR_OPPORTUNITY":
            timeline.append(
                {
                    "at": available_at.isoformat(),
                    "source": signal.get("source_type"),
                    "signal_id": str(signal.get("id")),
                    "title": signal.get("title"),
                    "decision": decision.decision,
                    "action": action,
                    "event_confidence": confidence,
                    "match_confidence": match_confidence,
                    "lifecycle_stage": candidate.get("lifecycle_stage"),
                }
            )

    matching = [opportunity for opportunity in opportunities if _matches_truth(opportunity, case)]
    first_relevant_at = min((value[0] for value in relevant_sources), default=None)
    coverage_complete = bool((case.get("provenance") or {}).get("source_coverage_complete"))
    if first_relevant_at is None and not coverage_complete:
        return _excluded_case(
            case,
            (
                "no case-linked historical planning/recruitment evidence; absence cannot be "
                "distinguished from incomplete source history"
            ),
        )
    first_opportunity_at = min((value["created_at"] for value in matching), default=None)
    lead_time = (outcome_at.date() - first_relevant_at.date()).days if first_relevant_at else None
    known_postcode = normalize_identity(case.get("known_postcode"))
    if len(matching) > 1:
        site_resolution = "DUPLICATE_OPPORTUNITY"
    elif matching and known_postcode:
        site_resolution = (
            "CORRECT"
            if normalize_identity(matching[0]["postcode"]) == known_postcode
            else "INCORRECT"
        )
    elif matching and case.get("known_location"):
        site_resolution = "PROBABLE_PROVIDER_AREA"
    elif matching:
        site_resolution = "UNRESOLVED_NO_SITE_TRUTH"
    else:
        site_resolution = "UNRESOLVED"

    # Only historically retrieved identity evidence may improve organisation resolution.
    company_improved = False
    for item in company_evidence:
        safe = company_identity_available(item, replay_end)
        if not safe:
            continue
        if case.get("known_company_number") and normalize_identity(
            safe.get("company_number")
        ) == normalize_identity(case.get("known_company_number")):
            operator_resolved_at = safe["available_at"]
            company_improved = first_operator_resolution not in {"EXACT", "STRONG"}
            first_operator_resolution = "EXACT"

    first_source = min(relevant_sources, default=(None, None))[1]
    source_dates = {
        source: min(
            (at for at, candidate_source in relevant_sources if candidate_source == source),
            default=None,
        )
        for source in ("planning", "recruitment")
    }
    matching_keys = {item["key"] for item in matching}
    for opportunity in opportunities:
        opportunity["truth_match"] = opportunity["key"] in matching_keys
    return {
        "benchmark_case_id": str(case["id"]),
        "case_key": case.get("benchmark_case_id"),
        "vertical": vertical,
        "outcome_type": case.get("outcome_type"),
        "outcome_date": outcome_at.date().isoformat(),
        "usable": True,
        "exclusion_reason": None,
        "detected": first_relevant_at is not None,
        "opportunity_created": first_opportunity_at is not None,
        "first_discovered_at": first_relevant_at.isoformat() if first_relevant_at else None,
        "first_opportunity_at": first_opportunity_at.isoformat() if first_opportunity_at else None,
        "first_source": first_source,
        "lead_time_days": lead_time,
        "organisation_resolution": first_operator_resolution,
        "organisation_resolved_at": operator_resolved_at.isoformat()
        if operator_resolved_at
        else None,
        "companies_house_improved_identity": company_improved,
        "site_resolution": site_resolution,
        "duplicate_opportunities": max(0, len(matching) - 1),
        "incorrect_merges": 0,
        "review_items": review_items,
        "source_dates": {
            key: value.isoformat() if value else None for key, value in source_dates.items()
        },
        "timeline": timeline,
        "generated_opportunities": opportunities,
        "confidence_metrics": {
            "event_confidence": max((item["event_confidence"] for item in matching), default=None),
            "match_confidence": max(
                (item.get("match_confidence") or 0 for item in matching), default=None
            ),
            "lifecycle_confidence": None,
            "commercial_priority": None,
        },
    }


def _excluded_case(case: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "benchmark_case_id": str(case["id"]),
        "case_key": case.get("benchmark_case_id"),
        "vertical": case.get("vertical"),
        "outcome_type": case.get("outcome_type"),
        "outcome_date": str(case.get("outcome_date") or ""),
        "usable": False,
        "exclusion_reason": reason,
        "detected": False,
        "opportunity_created": False,
        "review_items": 0,
        "timeline": [],
        "generated_opportunities": [],
        "confidence_metrics": {},
    }


def aggregate_results(results: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    usable = [item for item in results if item["usable"]]
    positives = [item for item in usable if item["outcome_type"] in POSITIVE_OUTCOMES]
    negatives = [item for item in usable if item["outcome_type"] in NEGATIVE_OUTCOMES]
    detected = [item for item in positives if item["detected"]]
    lead_times = [
        int(item["lead_time_days"]) for item in detected if item.get("lead_time_days") is not None
    ]
    generated = {
        item["key"]: item for result in usable for item in result.get("generated_opportunities", [])
    }
    true_keys = {
        item["key"]
        for result in positives
        for item in result.get("generated_opportunities", [])
        if item.get("truth_match")
    }
    false_keys = {
        item["key"]
        for result in negatives
        for item in result.get("generated_opportunities", [])
        if item.get("truth_match")
    }
    true_opportunities = len(true_keys)
    known_false = len(false_keys)
    labelled = true_opportunities + known_false
    unresolved_generated = max(0, len(generated) - labelled)
    site_truth = [
        item
        for item in positives
        if item.get("site_resolution")
        not in {None, "UNRESOLVED_NO_SITE_TRUTH", "PROBABLE_PROVIDER_AREA"}
    ]
    site_resolved = [item for item in site_truth if item.get("site_resolution") == "CORRECT"]
    organisation_resolved = [
        item for item in positives if item.get("organisation_resolution") in {"EXACT", "STRONG"}
    ]
    metrics = {
        "cases_attempted": len(results),
        "cases_usable": len(usable),
        "cases_excluded": len(results) - len(usable),
        "positive_cases": len(positives),
        "detected_cases": len(detected),
        "recall": round(len(detected) / len(positives), 4) if positives else None,
        "precision": round(true_opportunities / labelled, 4) if labelled and negatives else None,
        "precision_denominator": labelled,
        "precision_limitation": None if negatives else "no reliable negative benchmark cases",
        "known_false_opportunities": known_false,
        "unlabelled_opportunities": unresolved_generated,
        "lead_time_days": {
            "median": median(lead_times) if lead_times else None,
            "p25": _percentile(lead_times, 0.25),
            "p75": _percentile(lead_times, 0.75),
            "p90": _percentile(lead_times, 0.90) if len(lead_times) >= 4 else None,
        },
        "organisation_accuracy": round(len(organisation_resolved) / len(positives), 4)
        if positives
        else None,
        "site_accuracy": round(len(site_resolved) / len(site_truth), 4) if site_truth else None,
        "site_truth_cases": len(site_truth),
        "review_items": sum(item["review_items"] for item in usable),
        "reviews_per_genuine_opportunity": round(
            sum(item["review_items"] for item in positives) / len(positives), 3
        )
        if positives
        else None,
        "duplicate_opportunities": sum(item.get("duplicate_opportunities", 0) for item in usable),
        "incorrect_merges": sum(item.get("incorrect_merges", 0) for item in usable),
    }
    source_contribution: dict[str, Any] = {}
    for source in ("planning", "recruitment"):
        source_cases = [item for item in positives if item.get("source_dates", {}).get(source)]
        source_leads = [
            int(item["lead_time_days"])
            for item in source_cases
            if item.get("first_source") == source and item.get("lead_time_days") is not None
        ]
        source_contribution[source] = {
            "first_discoveries": sum(item.get("first_source") == source for item in positives),
            "cases_with_evidence": len(source_cases),
            "only_source": sum(
                bool(item.get("source_dates", {}).get(source))
                and not any(
                    item.get("source_dates", {}).get(other)
                    for other in ("planning", "recruitment")
                    if other != source
                )
                for item in positives
            ),
            "corroborations": sum(
                bool(item.get("source_dates", {}).get(source))
                and item.get("first_source") != source
                for item in positives
            ),
            "missed": len(positives) - len(source_cases),
            "median_lead_time_days": median(source_leads) if source_leads else None,
        }
    source_contribution["combined"] = {
        "found_by_either": sum(
            bool(
                item.get("source_dates", {}).get("planning")
                or item.get("source_dates", {}).get("recruitment")
            )
            for item in positives
        ),
        "both_sources_present": sum(
            bool(
                item.get("source_dates", {}).get("planning")
                and item.get("source_dates", {}).get("recruitment")
            )
            for item in positives
        ),
        "both_required_before_opportunity": 0,
        "neither_detected": sum(not item["detected"] for item in positives),
    }
    return metrics, source_contribution


def compare_metrics(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    def delta(path: tuple[str, ...]) -> float | None:
        a: Any = left
        b: Any = right
        for key in path:
            a = (a or {}).get(key)
            b = (b or {}).get(key)
        if a is None or b is None:
            return None
        return round(float(b) - float(a), 4)

    return {
        "recall": delta(("recall",)),
        "precision": delta(("precision",)),
        "median_lead_time_days": delta(("lead_time_days", "median")),
        "organisation_accuracy": delta(("organisation_accuracy",)),
        "site_accuracy": delta(("site_accuracy",)),
        "reviews_per_genuine_opportunity": delta(("reviews_per_genuine_opportunity",)),
    }

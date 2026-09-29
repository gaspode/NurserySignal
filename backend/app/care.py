from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.care_planning_review import (
    care_planning_decision_allows_fastpath,
    classify_care_planning_subtype,
)
from app.opportunity_policy import OpportunityCreationDecision
from app.planning import PlanningRecord, planning_record_from_signal
from app.planning_outcomes import canonical_planning_outcome
from app.recruitment import RecruitmentRecord, recruitment_record_from_signal

CARE_PLANNING_DIRECT = (
    r"\bchildren(?:['’]s|s)?\s+(?:residential\s+)?(?:care\s+)?home\b",
    r"\bresidential\s+(?:care\s+)?home\s+for\s+(?:children|young\s+people)\b",
    r"\bresidential\s+(?:care|accommodation)\s+for\s+(?:children|young\s+people)\b",
    r"\bhome\s+for\s+(?:up\s+to\s+\d+\s+)?(?:children|young\s+people)\b",
)
CARE_CHILD_CONTEXT = (
    r"\bchildren(?:['’]s|s)?\b",
    r"\byoung\s+people\b",
    r"\blooked[- ]after\s+children\b",
    r"\bchildren\s+in\s+care\b",
)
CARE_RESIDENTIAL_CONTEXT = (
    r"\bcare\s+home\b",
    r"\bresidential\s+(?:care|home|institution|accommodation)\b",
    r"\bclass\s+c2\b|\bc2\s+(?:use|residential)\b",
)
CARE_ADULT_EXCLUSIONS = (
    r"\b(?:elderly|older\s+people|older\s+persons|nursing)\s+(?:care\s+)?home\b",
    r"\badult(?:s|'s)?\s+(?:care|supported\s+living)\b",
    r"\bdementia\b",
    r"\bstudent\s+accommodation\b",
    r"\bhospital\b",
    r"\bboarding\s+school\b",
)
CARE_CHANGE_PATTERNS = {
    "EXPANSION": (
        r"\b(?:increase|increasing|increased)\b.{0,50}\b(?:occupancy|capacity|beds?|places?)\b",
        r"\b(?:extension|expand|expansion|additional)\b",
    ),
    "RELOCATION": (r"\b(?:relocat|new\s+premises|move\s+to|new\s+site)\w*\b",),
    "OPENING": (
        r"\b(?:change\s+of\s+use|conversion|convert|new\s+build|new\s+children|create|proposed)\w*\b",
        r"\bcertificate\s+of\s+lawfulness\b",
    ),
}
CARE_ROLE_PATTERNS = (
    ("registered_manager", r"\bregistered\s+manager\b"),
    ("responsible_individual", r"\bresponsible\s+individual\b"),
    ("deputy_manager", r"\bdeputy\s+manager\b"),
    ("senior_residential_support_worker", r"\bsenior\s+residential\s+support\s+worker\b"),
    ("residential_support_worker", r"\bresidential\s+(?:childcare\s+)?support\s+worker\b"),
    (
        "residential_childcare_worker",
        r"\bresidential\s+child\s*care\s+worker\b",
    ),
    (
        "childrens_support_worker",
        r"\bchildren(?:['’]s|s)?\s+support\s+worker\b",
    ),
    ("childrens_home_manager", r"\bchildren(?:['’]s|s)?\s+home\s+manager\b"),
    # Generic residential-care titles are meaningful only after the separate
    # children's-home setting check succeeds. Keeping them here allows the
    # advert body to establish the setting without making a bare Support
    # Worker/Team Leader title a CareSignal candidate.
    ("senior_support_worker", r"\bsenior\s+support\s+worker\b"),
    ("support_worker", r"\bsupport\s+(?:staff|worker)\b"),
    ("team_leader", r"\bteam\s+leader\b"),
)
CARE_RECRUITMENT_CHANGE = {
    "brand_new_home": r"\bbrand[- ]new\s+(?:children(?:['’]s|s)?\s+)?home\b",
    "new_childrens_home": r"\bnew\s+children(?:['’]s|s)?\s+home\b",
    "new_residential_home": (
        r"\b(?:brand[- ]new|new)\s+residential\s+"
        r"(?:children(?:['’]s|s)?\s+)?home\b"
    ),
    "newly_opened_home": (
        r"\bnewly\s+opened\s+(?:children(?:['’]s|s)?\s+|residential\s+)?home\b"
    ),
    "opening_soon": r"\bopening\s+soon\b",
    "preparing_to_open": (
        r"\b(?:prepar(?:e|es|ing)|getting\s+ready)\s+to\s+open\b"
        r"|\bdue\s+to\s+open\b"
    ),
    "pre_registration": r"\bpre[- ]registration\b|\bthrough\s+(?:ofsted\s+)?registration\b",
    "registration_stage": (
        r"\b(?:going|lead(?:ing)?|support(?:ing)?|work(?:ing)?)\b.{0,35}"
        r"\b(?:through\s+)?ofsted\s+registration\b"
        r"|\bsupport\b.{0,25}\bregistration\s+process\b"
    ),
    "launch_home": r"\b(?:launch|open|opening)\s+(?:our\s+|the\s+)?(?:new\s+)?home\b",
    "founding_team": r"\bfounding\s+team\b.{0,80}\b(?:home|service)\b",
    "new_service": r"\bnew\s+service\b",
    "additional_home": (
        r"\b(?:first|second|third|additional|another)\s+"
        r"(?:children(?:['’]s|s)?\s+)?home\b"
    ),
    "new_property": (
        r"\bnewly\s+acquired\s+(?:home|property|premises)\b"
        r"|\bacquired\s+(?:a\s+)?new\s+(?:home|property|premises)\b"
    ),
    "expanding_to_home": (
        r"\bexpand(?:ing)?\s+(?:in)?to\s+(?:a\s+)?new\s+home\b"
        r"|\bopening\s+another\s+home\b"
    ),
    "new_provision": r"\bnew\s+(?:residential\s+)?provision\b",
}


@dataclass(frozen=True)
class CarePlanningDecision:
    matched: bool
    confidence: float
    change_type: str
    reasons: tuple[str, ...]
    exclusions: tuple[str, ...]


def _joined(*values: Any) -> str:
    return " ".join(str(value or "") for value in values).lower()


def classify_care_planning(record: PlanningRecord) -> CarePlanningDecision:
    proposal = _joined(
        record.description,
        record.status,
        record.decision,
        (record.raw or {}).get("description"),
        (record.raw or {}).get("category"),
        (record.raw or {}).get("planning_route"),
    )
    direct = [pattern for pattern in CARE_PLANNING_DIRECT if re.search(pattern, proposal)]
    child = [pattern for pattern in CARE_CHILD_CONTEXT if re.search(pattern, proposal)]
    residential = [pattern for pattern in CARE_RESIDENTIAL_CONTEXT if re.search(pattern, proposal)]
    exclusions = [pattern for pattern in CARE_ADULT_EXCLUSIONS if re.search(pattern, proposal)]
    matched = bool(direct or (child and residential)) and not exclusions
    change_type = "OTHER_CHANGE"
    if matched:
        for candidate, patterns in CARE_CHANGE_PATTERNS.items():
            if any(re.search(pattern, proposal) for pattern in patterns):
                change_type = candidate
                break
    confidence = 0.15
    if matched:
        confidence = 0.9 if direct else 0.78
        if change_type == "EXPANSION":
            confidence = max(confidence, 0.88)
    reasons = tuple(
        item
        for item, present in (
            ("explicit children-home wording", bool(direct)),
            ("children/young-people context", bool(child)),
            ("residential-care context", bool(residential)),
        )
        if present
    )
    return CarePlanningDecision(matched, confidence, change_type, reasons, tuple(exclusions))


def classify_care_recruitment(record: RecruitmentRecord) -> dict[str, Any]:
    title = record.title.lower()
    setting_text = _joined(record.employer_name, record.workplace_name, record.description)
    full_text = _joined(title, setting_text)
    roles = [name for name, pattern in CARE_ROLE_PATTERNS if re.search(pattern, title)]
    child_context = any(re.search(pattern, full_text) for pattern in CARE_CHILD_CONTEXT)
    home_context = bool(
        re.search(r"\b(?:residential\s+)?children(?:['’]s|s)?\s+home\b", full_text)
        or (
            child_context
            and re.search(r"\bresidential\s+(?:care|home|child\s*care)\b", full_text)
        )
    )
    adult_exclusions = [
        pattern for pattern in CARE_ADULT_EXCLUSIONS if re.search(pattern, full_text)
    ]
    changes = [
        name for name, pattern in CARE_RECRUITMENT_CHANGE.items() if re.search(pattern, full_text)
    ]
    matched = bool(roles and home_context and not adult_exclusions)
    relevance = (
        "RELEVANT_CHANGE"
        if matched and changes
        else "RELEVANT_ROUTINE"
        if matched
        else "IRRELEVANT"
    )
    confidence = 0.18
    if matched:
        confidence = 0.82
        if roles[0] in {"registered_manager", "childrens_home_manager", "responsible_individual"}:
            confidence += 0.06
        if changes:
            confidence += 0.06
        confidence = min(confidence, 0.96)
    return {
        "matched": matched,
        "role_category": roles[0] if roles else "other",
        "role_categories": roles,
        "setting_category": "childrens_home" if home_context else "unknown",
        "setting_categories": ["childrens_home"] if home_context else [],
        "relevance": relevance,
        "commercial_change_evidence": "STRONG" if changes else "NONE",
        "explicit_change_terms": changes,
        "exclusions": adult_exclusions,
        "confidence": confidence,
        "likely_false_positive": bool(adult_exclusions),
        "matched_role_terms": roles,
        "matched_setting_terms": ["childrens_home"] if home_context else [],
        "ambiguity_flags": [],
    }


def care_planning_signal(
    record: PlanningRecord,
    decision: CarePlanningDecision,
    *,
    historical_source_date: bool = False,
) -> dict[str, Any]:
    from app.planning import planning_signal

    class CompatibleDecision:
        positive_terms = decision.reasons
        exclusions = decision.exclusions
        school_nursery = False
        childcare_terms: tuple[str, ...] = ()
        horticultural_terms: tuple[str, ...] = ()

    signal = planning_signal(  # type: ignore[arg-type]
        record, CompatibleDecision(), historical_source_date=historical_source_date
    )
    signal["vertical"] = "CHILDRENS_HOME"
    signal["metadata"]["care_classification"] = {
        "matched": decision.matched,
        "confidence": decision.confidence,
        "change_type": decision.change_type,
        "reasons": list(decision.reasons),
        "exclusions": list(decision.exclusions),
    }
    signal["metadata"]["location_sensitivity"] = "INTERNAL_EXACT"
    return signal


def care_recruitment_signal(record: RecruitmentRecord, decision: dict[str, Any]) -> dict[str, Any]:
    from app.recruitment import recruitment_signal

    signal = recruitment_signal(record, decision)
    signal["vertical"] = "CHILDRENS_HOME"
    signal["metadata"]["care_recruitment_classification"] = decision
    signal["metadata"]["location_sensitivity"] = "INTERNAL_EXACT"
    return signal


def enrich_care_signal(raw: dict[str, Any]) -> dict[str, Any]:
    source_type = str(raw.get("source_type") or "").lower()
    metadata = raw.get("metadata") or {}
    if source_type == "planning":
        decision = classify_care_planning(planning_record_from_signal(raw))
        planning_outcome = canonical_planning_outcome(metadata)
        planning_subtype = classify_care_planning_subtype(raw)
        matched = decision.matched
        change_type = decision.change_type
        relevance = (
            "RELEVANT_CHANGE"
            if matched and change_type != "OTHER_CHANGE"
            else "RELEVANT_ROUTINE"
            if matched
            else "IRRELEVANT"
        )
        confidence = decision.confidence
        role = None
        setting = "childrens_home" if matched else "unknown"
        changes: list[str] = []
    elif source_type == "recruitment":
        decision = classify_care_recruitment(recruitment_record_from_signal(raw))
        matched = decision["matched"]
        relevance = decision["relevance"]
        change_type = (
            "OPENING" if decision["commercial_change_evidence"] == "STRONG" else "OTHER_CHANGE"
        )
        confidence = decision["confidence"]
        role = decision["role_category"]
        setting = decision["setting_category"]
        changes = decision["explicit_change_terms"]
    elif source_type == "ofsted":
        registration_status = str(metadata.get("registration_status") or "").upper()
        matched = bool(metadata.get("ofsted_urn") and raw.get("organisation_hint"))
        relevance = "RELEVANT_ROUTINE" if matched else "UNCERTAIN"
        change_type = "OTHER_CHANGE"
        confidence = 0.92 if registration_status in {"ACTIVE", "REGISTERED"} else 0.75
        role = None
        setting = "childrens_home"
        changes = []
    elif source_type == "procurement":
        category = str(metadata.get("procurement_category") or "UNCERTAIN")
        confidence = float(metadata.get("procurement_confidence") or 0.5)
        matched = category != "IRRELEVANT"
        relevance = (
            "RELEVANT_CHANGE"
            if category
            in {
                "NEW_HOME_COMMISSIONING",
                "NEW_CAPACITY_MARKET_ENGAGEMENT",
                "OPERATOR_PROCUREMENT",
                "CONTRACT_AWARD",
            }
            else "RELEVANT_ROUTINE"
            if category
            in {"ROUTINE_PLACEMENT_FRAMEWORK", "EXISTING_SERVICE_REPROCUREMENT"}
            else "UNCERTAIN"
        )
        change_type = "OPENING" if relevance == "RELEVANT_CHANGE" else "OTHER_CHANGE"
        role = None
        setting = "childrens_home" if matched else "unknown"
        changes = [category] if relevance == "RELEVANT_CHANGE" else []
    else:
        raise ValueError("unsupported CareSignal source type")
    material_planning_change = source_type == "planning" and change_type != "OTHER_CHANGE"
    commercial_change = "STRONG" if material_planning_change or changes else "NONE"
    action = (
        "REVIEW"
        if source_type == "procurement" and matched
        else
        "CREATE_OPPORTUNITY"
        if matched
        and (
            material_planning_change
            or (source_type == "recruitment" and relevance == "RELEVANT_CHANGE")
        )
        else "SUPPORT_EXISTING_ONLY"
        if matched
        else "IGNORE_FOR_OPPORTUNITY"
    )
    if source_type == "planning":
        if planning_subtype.subtype in {"REFUSED", "WITHDRAWN"}:
            action = "IGNORE_FOR_OPPORTUNITY"
            commercial_change = "NONE"
        elif planning_subtype.subtype in {
            "LAWFULNESS_EXISTING",
            "CONDITION_VARIATION",
            "CONDITION_DISCHARGE",
            "NON_MATERIAL_AMENDMENT",
            "FOLLOW_UP_OTHER",
        }:
            action = "SUPPORT_EXISTING_ONLY" if matched else "IGNORE_FOR_OPPORTUNITY"
            commercial_change = (
                "STRONG"
                if planning_subtype.material_capacity_change
                and planning_subtype.subtype == "CONDITION_VARIATION"
                else "NONE"
            )
        elif planning_subtype.subtype == "LAWFULNESS_PROPOSED":
            action = "CREATE_OPPORTUNITY" if matched else "REVIEW"
        elif planning_subtype.subtype == "AMBIGUOUS":
            action = "REVIEW" if matched else "IGNORE_FOR_OPPORTUNITY"
    creation_reason = (
        "Planning evidence indicates expansion of an existing children's home"
        if source_type == "planning" and change_type == "EXPANSION"
        else "Planning evidence indicates a new children's home"
        if material_planning_change
        else "Recruitment explicitly indicates a new or opening children's home"
        if action == "CREATE_OPPORTUNITY"
        else "Ofsted regulatory evidence can confirm registration of an existing opportunity"
        if source_type == "ofsted"
        else "Procurement evidence is retained for shadow evaluation only"
        if source_type == "procurement"
        else "Relevant evidence supports an existing CareSignal opportunity only"
    )
    facts = {
        "method": "care-deterministic-v2",
        "classification": (
            "care-planning"
            if source_type == "planning"
            else "care-recruitment"
            if source_type == "recruitment"
            else "care-regulatory"
            if source_type == "ofsted"
            else "care-procurement"
        ),
        "source_type": source_type,
        "planning_candidate_matched": matched if source_type == "planning" else None,
        "planning_outcome": (
            planning_outcome.outcome.value if source_type == "planning" else None
        ),
        "planning_outcome_value": (
            planning_outcome.matched_value if source_type == "planning" else None
        ),
        "planning_outcome_field": (
            planning_outcome.matched_field if source_type == "planning" else None
        ),
        "planning_subtype": planning_subtype.subtype if source_type == "planning" else None,
        "planning_subtype_reasons": (
            list(planning_subtype.reasons) if source_type == "planning" else []
        ),
        "explicit_new_home_proposal": (
            planning_subtype.explicit_new_home if source_type == "planning" else False
        ),
        "planning_decision_fastpath_eligible": (
            care_planning_decision_allows_fastpath(metadata) if source_type == "planning" else False
        ),
        "planning_material_capacity_change": (
            planning_subtype.material_capacity_change if source_type == "planning" else False
        ),
        "planning_prior_references": (
            list(planning_subtype.prior_application_references) if source_type == "planning" else []
        ),
        "planning_ambiguity_markers": (
            list(planning_subtype.reasons)
            if source_type == "planning" and planning_subtype.subtype == "AMBIGUOUS"
            else []
        ),
        "children_home_relevance": relevance,
        "recruitment_relevance": relevance if source_type == "recruitment" else "unknown",
        "commercial_change_evidence": commercial_change,
        "opportunity_creation_decision": action,
        "opportunity_change_type": change_type,
        "opportunity_creation_reason": creation_reason,
        "care_role_category": role,
        "care_setting_category": setting,
        "care_explicit_change_terms": changes,
        "likely_false_positive": not matched,
        "location_sensitivity": "INTERNAL_EXACT",
        "ofsted_urn": metadata.get("ofsted_urn"),
        "registration_status": metadata.get("registration_status"),
        "procurement_category": metadata.get("procurement_category"),
        "procurement_reason": metadata.get("procurement_reason"),
        "procurement_platform": metadata.get("procurement_platform"),
        "procurement_evaluation_mode": (
            "SHADOW_ONLY" if source_type == "procurement" else None
        ),
    }
    title = str(raw.get("title") or "Children's home signal")
    operator = raw.get("organisation_hint")
    return {
        "raw_signal_id": raw["id"],
        "schema_version": "1.0",
        "source_type": source_type,
        "raw_text": raw.get("raw_text"),
        "metadata": metadata,
        "event_type": (
            "expansion"
            if change_type == "EXPANSION"
            else "opening"
            if action == "CREATE_OPPORTUNITY"
            else "other"
        ),
        "nursery_name": operator or title,
        "operator_name": operator,
        "address": raw.get("location_hint"),
        "expected_opening_date": None,
        "capacity": None,
        "lifecycle_stage": (
            "PLANNING"
            if source_type == "planning"
            else "RECRUITING"
            if source_type == "recruitment"
            else "REGISTRATION"
            if source_type == "ofsted"
            else "DISCOVERED"
        ),
        "confidence": confidence,
        "extracted_facts": facts,
        "evidence": {
            "source_url": raw.get("source_url"),
            "title": title,
            "location_hint": raw.get("location_hint"),
        },
    }


def care_opportunity_decision(candidate: dict[str, Any]) -> OpportunityCreationDecision:
    facts = candidate.get("extracted_facts") or {}
    return OpportunityCreationDecision(
        str(facts.get("opportunity_creation_decision") or "REVIEW"),
        str(facts.get("opportunity_change_type") or "OTHER_CHANGE"),
        str(facts.get("opportunity_creation_reason") or "CareSignal evidence requires review"),
    )


def care_opportunity_title(candidate: dict[str, Any], decision: OpportunityCreationDecision) -> str:
    operator = str(candidate.get("operator_name") or "").strip()
    address = str(candidate.get("address") or "").strip()
    postcode = str((candidate.get("metadata") or {}).get("postcode") or "").strip()
    subject = operator if operator and len(operator) <= 100 else address[:100] or "Children's home"
    label = {
        "OPENING": "New children's home",
        "EXPANSION": "Children's home expansion",
        "RELOCATION": "Children's home relocation",
    }.get(decision.change_type, "Children's home change")
    return f"{label} — {subject}" + (
        f" ({postcode})" if postcode and postcode.lower() not in subject.lower() else ""
    )

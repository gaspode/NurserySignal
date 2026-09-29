from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

PLANNING_OUTCOME_POLICY_VERSION = "planning-outcome-v1"


class PlanningOutcome(StrEnum):
    REFUSED = "REFUSED"
    WITHDRAWN = "WITHDRAWN"
    APPROVED = "APPROVED"
    REFUSED_UNDER_APPEAL = "REFUSED_UNDER_APPEAL"
    APPEAL_ALLOWED = "APPEAL_ALLOWED"
    APPEAL_DISMISSED = "APPEAL_DISMISSED"
    PENDING = "PENDING"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PlanningOutcomeAssessment:
    outcome: PlanningOutcome
    matched_value: str | None = None
    matched_field: str | None = None
    decision_date: str | None = None
    structured_values: tuple[tuple[str, str], ...] = ()

    @property
    def refused(self) -> bool:
        return self.outcome in {PlanningOutcome.REFUSED, PlanningOutcome.APPEAL_DISMISSED}

    @property
    def withdrawn(self) -> bool:
        return self.outcome is PlanningOutcome.WITHDRAWN

    @property
    def terminal_negative(self) -> bool:
        return self.refused or self.withdrawn


def normalize_structured_planning_value(value: Any) -> str:
    if not isinstance(value, (str, int, float)):
        return ""
    return re.sub(r"[^A-Z0-9]+", " ", str(value).upper()).strip()


def _nested(mapping: dict[str, Any], *keys: str) -> Any:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def structured_planning_fields(metadata: Any) -> tuple[tuple[str, Any], ...]:
    data = metadata if isinstance(metadata, dict) else {}
    return (
        ("decision", data.get("decision")),
        ("planning_status", data.get("planning_status")),
        ("status", data.get("status")),
        (
            "provider_record.decision.outcome",
            _nested(data, "provider_record", "decision", "outcome"),
        ),
        ("provider_record.status", _nested(data, "provider_record", "status")),
    )


def structured_planning_values(metadata: Any) -> tuple[Any, ...]:
    return tuple(value for _, value in structured_planning_fields(metadata))


_ACTIVE_APPEAL = re.compile(
    r"^(?:APPEAL (?:LODGED|STARTED|PENDING|IN PROGRESS|VALIDATED)|"
    r"APPEAL LODGED REFUSE)$"
)
_APPEAL_ALLOWED = re.compile(r"^APPEAL (?:ALLOWED|SUCCESSFUL|UPHELD)$")
_APPEAL_DISMISSED = re.compile(r"^APPEAL (?:DISMISSED|REFUSED|REJECTED)$")

_REFUSED = (
    re.compile(r"^(?:REFUSAL|REFUSED|REJECTED)$"),
    re.compile(r"^REFUSAL (?:FULL|OF PERMISSION|OF CONSENT)$"),
    re.compile(r"^REFUSE(?: PERMISSION(?: CONSENT)?| CONSENT)?$"),
    re.compile(r"^REFUSED (?:LUC|CLU|INFORMATIVE|INFORMATIVES)$"),
    re.compile(r"^(?:FULL REF|DECIDED REFUSE)$"),
    re.compile(
        r"^(?:PLANNING PERMISSION|PERMISSION|PERMISSION CONSENT|APPLICATION|DM APPLICATION|"
        r"LAWFUL USE|PROPOSED LAWFULNESS) REFUSED(?: INFORMATIVE| INFORMATIVES)?$"
    ),
    re.compile(
        r"^CERTIFICATE REFUSED(?: LAWFUL DEV CERT)?$|"
        r"^CERTIFICATE OF LAWFULNESS REFUSED$"
    ),
    # Some councils append standard decision-note prose to the structured
    # outcome. Anchor the authoritative prefix rather than searching arbitrary
    # proposal text for the word "refused".
    re.compile(r"^FULL APPLICATION REFUSED(?: CONDITIONS OR REASONS\b.*)?$"),
)

_WITHDRAWN = (
    re.compile(r"^WITHDRAWN$"),
    re.compile(r"^WITHDRAWN (?:P|APPLICANT|APPLICATION|BY APPLICANT|AFTER REGISTRATION)$"),
    re.compile(r"^APPLICATION WITHDRAWN(?: BY APPLICANT)?$"),
)

_APPROVED = (
    re.compile(r"^(?:APPROVE|APPROVED|APPROVAL|GRANT|GRANTED|PERMIT|PERMITTED|ALLOWED|LAWFUL)$"),
    re.compile(r"^(?:APPLICATION )?(?:APPROVED|GRANTED|PERMITTED)(?: APPROVED)?$"),
    re.compile(
        r"^(?:APPROVE|APPROVED|APPROVAL|GRANT|GRANTED) "
        r"(?:WITH|SUBJECT TO) CONDITIONS?(?: INFORMATIVES)?$"
    ),
    re.compile(r"^APPROVAL OF DETAILS FOR CONDITIONS$"),
    re.compile(r"^APPROVED BNG NOT REQUIRED$"),
    re.compile(r"^(?:GRANT|GRANTED) CONDITIONAL(?:LY)?(?: PERMISSION)?$"),
    re.compile(r"^GRANT PERMISSION(?: SUBJECT TO CONDITIONS)?$"),
    re.compile(
        r"^(?:CONDITIONAL APPROVAL|CONDITIONALLY APPROVED)"
        r"(?: DELEGATED| PD REMOVED)?$"
    ),
    re.compile(r"^(?:PLANNING PERMISSION GRANTED|GRANTED PERMISSION)$"),
    re.compile(r"^(?:PROPOSED LAWFULNESS|CERTIFICATE|CLOPUD) GRANTED$"),
    re.compile(r"^GRANT (?:LAWFUL USE|SECTION 191 192) CERTIFICATE$"),
    re.compile(r"^APPROVAL (?:FULL|CLU)$"),
    re.compile(r"^DECIDED APPROVE SUBJECT TO CONDITIONS$"),
    re.compile(r"^(?:DECIDED|FINAL DECISION) APPROVED$"),
    re.compile(r"^DECIDED GRANTED$"),
    re.compile(r"^PER APPLICATION PERMITTED$"),
    re.compile(r"^FULL APPLICATION GRANTED(?: CONDITIONS OR REASONS\b.*)?$"),
)

_PENDING = (
    re.compile(r"^(?:PENDING|LIVE|VALID|UNDECIDED|APPLICATION UNDECIDED)$"),
    re.compile(r"^(?:AWAITING DECISION|UNDER CONSIDERATION|PENDING CONSIDERATION)$"),
    re.compile(r"^(?:REGISTERED|REGISTERED APPLICATION|REGISTERED UNDER ASSESSMENT)$"),
)


def _first_match(
    values: tuple[tuple[str, str], ...], patterns: tuple[re.Pattern[str], ...]
) -> tuple[str, str] | None:
    for field, value in values:
        if any(pattern.fullmatch(value) for pattern in patterns):
            return field, value
    return None


def canonical_planning_outcome(metadata: Any) -> PlanningOutcomeAssessment:
    """Interpret only authoritative structured outcome/status fields.

    Proposal/title prose is deliberately excluded. Appeal lifecycle has
    precedence over the original decision so an appealed refusal is retained
    as active lifecycle evidence rather than cleaned up as a final refusal.
    """
    data = metadata if isinstance(metadata, dict) else {}
    values: list[tuple[str, str]] = []
    for field, raw_value in structured_planning_fields(data):
        normalized = normalize_structured_planning_value(raw_value)
        if normalized and (field, normalized) not in values:
            values.append((field, normalized))
    structured = tuple(values)
    decision_date = data.get("decision_date") or _nested(data, "provider_record", "date_decided")
    date_value = str(decision_date or "") or None

    appeal_allowed = _first_match(structured, (_APPEAL_ALLOWED,))
    if appeal_allowed:
        return PlanningOutcomeAssessment(
            PlanningOutcome.APPEAL_ALLOWED,
            appeal_allowed[1],
            appeal_allowed[0],
            date_value,
            structured,
        )
    appeal_dismissed = _first_match(structured, (_APPEAL_DISMISSED,))
    if appeal_dismissed:
        return PlanningOutcomeAssessment(
            PlanningOutcome.APPEAL_DISMISSED,
            appeal_dismissed[1],
            appeal_dismissed[0],
            date_value,
            structured,
        )
    active_appeal = _first_match(structured, (_ACTIVE_APPEAL,))
    refused = _first_match(structured, _REFUSED)
    if active_appeal and (refused or active_appeal[1].endswith(" REFUSE")):
        return PlanningOutcomeAssessment(
            PlanningOutcome.REFUSED_UNDER_APPEAL,
            active_appeal[1],
            active_appeal[0],
            date_value,
            structured,
        )

    withdrawn = _first_match(structured, _WITHDRAWN)
    if withdrawn:
        return PlanningOutcomeAssessment(
            PlanningOutcome.WITHDRAWN,
            withdrawn[1],
            withdrawn[0],
            date_value,
            structured,
        )
    if refused:
        return PlanningOutcomeAssessment(
            PlanningOutcome.REFUSED,
            refused[1],
            refused[0],
            date_value,
            structured,
        )
    approved = _first_match(structured, _APPROVED)
    if approved:
        return PlanningOutcomeAssessment(
            PlanningOutcome.APPROVED,
            approved[1],
            approved[0],
            date_value,
            structured,
        )
    pending = _first_match(structured, _PENDING)
    if pending:
        return PlanningOutcomeAssessment(
            PlanningOutcome.PENDING,
            pending[1],
            pending[0],
            date_value,
            structured,
        )
    return PlanningOutcomeAssessment(
        PlanningOutcome.UNKNOWN,
        decision_date=date_value,
        structured_values=structured,
    )

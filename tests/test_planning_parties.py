from __future__ import annotations

from app.customer import classify_customer_operator_identity
from app.planning_parties import extract_planning_party_provenance


def test_extracts_explicit_applicant_company_with_provenance() -> None:
    provenance, category = extract_planning_party_provenance(
        {"provider_record": {"applicant": "Example Care Ltd", "id": "planning-1"}},
        raw_source_identifier="plota:planning-1",
    )
    assert category == "APPLICANT_COMPANY_EXTRACTED"
    assert provenance["applicant"] == {
        "role": "APPLICANT",
        "name": "Example Care Ltd",
        "party_type": None,
        "company_like": True,
        "company_number": None,
        "source_path": "metadata.provider_record.applicant",
    }
    assert provenance["agent"] is None


def test_extracts_person_applicant_and_agent_without_collapsing_roles() -> None:
    provenance, category = extract_planning_party_provenance(
        {"applicant": "Jane Smith", "agent": "Planning Agent LLP"},
        raw_source_identifier="plota:planning-2",
    )
    assert category == "APPLICANT_AND_AGENT"
    assert provenance["applicant"]["role"] == "APPLICANT"
    assert provenance["applicant"]["company_like"] is False
    assert provenance["agent"]["role"] == "AGENT"


def test_agent_only_and_generic_organisation_hint_are_not_promoted() -> None:
    provenance, category = extract_planning_party_provenance(
        {"agent": "Planning Agent LLP", "organisation_hint": "Unlabelled Care Ltd"}
    )
    assert category == "AGENT_ONLY"
    assert provenance["applicant"] is None
    assert provenance["agent"]["name"] == "Planning Agent LLP"


def test_missing_or_malformed_party_metadata_is_safe() -> None:
    none, malformed = extract_planning_party_provenance({"provider_record": []})
    empty, no_identity = extract_planning_party_provenance({"organisation_hint": "Care Ltd"})
    assert none is None and malformed == "MALFORMED_OR_UNSUPPORTED_RAW"
    assert empty["applicant"] is None and no_identity == "NO_ROLE_LABELLED_IDENTITY"


def test_exact_structured_applicant_can_become_safe_operator_link_but_agent_cannot() -> None:
    provenance, _ = extract_planning_party_provenance({"applicant": "Example Care Ltd"})
    safe = classify_customer_operator_identity(
        applicants=[provenance["applicant"]["name"]],
        agents=[],
        organisations_by_identity={
            "example care ltd": [{"id": "operator-1", "name": "Example Care Ltd"}]
        },
    )
    agent_only = classify_customer_operator_identity(
        applicants=[],
        agents=["Example Care Ltd"],
        organisations_by_identity={
            "example care ltd": [{"id": "operator-1", "name": "Example Care Ltd"}]
        },
    )
    assert safe["automation_allowed"] is True
    assert agent_only["automation_allowed"] is False


def test_case_officer_is_preserved_without_becoming_identity_evidence() -> None:
    provenance, _ = extract_planning_party_provenance(
        {"provider_record": {"case_officer": "Council Officer", "agent_email": "a@example.test"}}
    )
    assert provenance["case_officer"]["role"] == "CASE_OFFICER"
    assert provenance["applicant"] is None
    assert provenance["contact_data"]["agent_email_available"] is True

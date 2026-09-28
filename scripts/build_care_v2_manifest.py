#!/usr/bin/env python3
"""Build the reviewed Care Ofsted v2 manifest from an admin-safe outcome export.

The script copies the immutable v1 research unchanged, adds bounded reviewed v2
findings, and records explicit no-result searches for every additional outcome. It
does not call production APIs or write live SignalHub data.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

NEW_RECORDS: list[dict[str, Any]] = [
    {
        "vertical": "CHILDRENS_HOME",
        "source_type": "planning",
        "provider": "OFFICIAL_COUNCIL_PLANNING",
        "external_id": "23/01003/FUL",
        "source_url": (
            "https://democracy.melton.gov.uk/documents/s26759/"
            "4.3%20-%2023.01003.FUL%20Old%20Ivy%20House%20Barkestone%20Le%20Vale.pdf"
        ),
        "title": "Change of use to a children's home for up to four young people",
        "raw_text": (
            "Change of use of existing dwellinghouse (Class C3) to a children's home "
            "(Class C2) for up to four young people. Applicant: Anchor Care and "
            "Education Ltd."
        ),
        "organisation_hint": "Anchor Care and Education Ltd",
        "location_hint": "Barkestone Le Vale, Leicestershire",
        "source_event_at": "2024-08-01T00:00:00Z",
        "available_at": "2024-08-01T00:00:00Z",
        "retrieved_at": "2026-09-28T00:00:00Z",
        "metadata": {
            "application_reference": "23/01003/FUL",
            "publication_date": "2024-08-01",
            "local_authority": "Leicestershire",
            "council": "Melton Borough Council",
        },
        "provenance": {
            "authority": "Melton Borough Council",
            "official": True,
            "availability_basis": "dated Planning Committee report",
            "research_version": "care-historical-research-v2",
        },
    },
    {
        "vertical": "CHILDRENS_HOME",
        "source_type": "planning",
        "provider": "PLOTA",
        "external_id": "S1BVP2NRM2M00",
        "source_url": (
            "https://docs.planning.org.uk/20231016/229/S1BVP2NRM2M00/"
            "wf61qn6qk35qz7nr.pdf"
        ),
        "title": "Proposed change of use to a children's home",
        "raw_text": (
            "Planning Management Statement for Care 4 Good Ltd regarding a proposed "
            "change of use to Class C2 at Yew Tree Lane, Rowley Regis, Sandwell. Care "
            "4 Good was established to provide specialist support and care for looked "
            "after young people aged 8 to 17."
        ),
        "organisation_hint": "Care 4 Good Ltd",
        "location_hint": "Rowley Regis, Sandwell",
        "source_event_at": "2023-10-16T00:00:00Z",
        "available_at": "2023-10-16T00:00:00Z",
        "retrieved_at": "2026-09-28T00:00:00Z",
        "metadata": {
            "publication_date": "2023-10-16",
            "local_authority": "Sandwell",
            "provider_document_key": "S1BVP2NRM2M00",
        },
        "provenance": {
            "authority": "Plota preserved planning document",
            "official": True,
            "availability_basis": "date-versioned planning document path",
            "research_version": "care-historical-research-v2",
        },
    },
]


NEW_LINKS = {
    "ofsted:2775281": {
        "provider": "OFFICIAL_COUNCIL_PLANNING",
        "source_type": "planning",
        "external_id": "23/01003/FUL",
        "case_link_confidence": "STRONG_CASE_LINK",
        "eligibility": "ELIGIBLE",
        "link_reason": (
            "Exact provider, Leicestershire geography and explicit children's-home "
            "change of use before registration."
        ),
    },
    "ofsted:2774759": {
        "provider": "PLOTA",
        "source_type": "planning",
        "external_id": "S1BVP2NRM2M00",
        "case_link_confidence": "STRONG_CASE_LINK",
        "eligibility": "ELIGIBLE",
        "link_reason": (
            "Exact provider, exact Sandwell authority and explicit C2 children's-home "
            "proposal before registration."
        ),
    },
}


def _outcome_items(value: dict[str, Any]) -> list[dict[str, Any]]:
    if "body" in value:
        value = json.loads(value["body"])
    items = value.get("items") or []
    return sorted(
        items,
        key=lambda item: (
            str((item.get("metadata") or {}).get("registration_date") or ""),
            str((item.get("metadata") or {}).get("ofsted_urn") or ""),
        ),
        reverse=True,
    )[:50]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--outcomes", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    outcomes = _outcome_items(json.loads(args.outcomes.read_text(encoding="utf-8")))
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    baseline_cases = {item["benchmark_case_id"]: item for item in baseline["cases"]}
    selected_keys = {
        f"ofsted:{item['metadata']['ofsted_urn']}" for item in outcomes
    }
    records = list(baseline["records"])
    records.extend(NEW_RECORDS)
    cases: list[dict[str, Any]] = []
    for outcome in outcomes:
        metadata = outcome["metadata"]
        key = f"ofsted:{metadata['ofsted_urn']}"
        if key in baseline_cases:
            cases.append(baseline_cases[key])
            continue
        candidate = NEW_LINKS.get(key)
        cases.append(
            {
                "benchmark_case_id": key,
                "status": "COMPLETE",
                "sources_searched": [
                    "SIGNALHUB_PRESERVED_PLANNING",
                    "SIGNALHUB_PRESERVED_RECRUITMENT",
                    "PLOTA_HISTORICAL",
                    "OFFICIAL_COUNCIL_PLANNING",
                    "OFFICIAL_RECRUITMENT",
                ],
                "search_strategy": {
                    "provider_terms": [outcome.get("organisation_hint")],
                    "location": metadata.get("local_authority"),
                    "window_months": 18,
                    "bounded": True,
                },
                "not_found_reason": None if candidate else "NOT_FOUND",
                "notes": (
                    "One official, date-verifiable, strong case link was retained."
                    if candidate
                    else (
                        "No planning or recruitment record with both a defensible public date "
                        "and a VERIFIED/STRONG site-level case link was found. Provider-only "
                        "or same-authority hits were not admitted because Ofsted redacts the home."
                    )
                ),
                "candidates": [candidate] if candidate else [],
                "researched_at": "2026-09-28T00:00:00Z",
            }
        )
    manifest = {
        "benchmark_version": "care-ofsted-v2",
        "corpus_version": "care-historical-research-v2",
        "vertical": "CHILDRENS_HOME",
        "researched_at": "2026-09-28T00:00:00Z",
        "research_method": {
            "bounded": True,
            "outcomes_selected": len(outcomes),
            "baseline_cases_preserved": len(selected_keys & set(baseline_cases)),
            "matcher_or_classifier_changed": False,
            "notes": (
                "Known outcomes guided source research only. Ofsted truth and later enrichment "
                "are not replay inputs."
            ),
        },
        "records": records,
        "cases": cases,
    }
    args.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Fetch a bounded, non-ingesting Plota corpus for historical benchmark research."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "backend"))

from app.planning import PlanningQuery, PlotaProvider  # noqa: E402
from app.secrets import provider_api_key_from_secret  # noqa: E402


def month_windows(start: date, end: date) -> list[tuple[date, date]]:
    windows: list[tuple[date, date]] = []
    cursor = start.replace(day=1)
    while cursor <= end:
        next_month = (
            date(cursor.year + 1, 1, 1)
            if cursor.month == 12
            else date(cursor.year, cursor.month + 1, 1)
        )
        windows.append((max(start, cursor), min(end, date.fromordinal(next_month.toordinal() - 1))))
        cursor = next_month
    return windows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--secret-arn", required=True)
    parser.add_argument("--from-date", type=date.fromisoformat, required=True)
    parser.add_argument("--to-date", type=date.fromisoformat, required=True)
    parser.add_argument("--term", action="append", required=True)
    parser.add_argument("--max-records-per-window", type=int, default=500)
    parser.add_argument("--page-size", type=int, default=250)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.to_date < args.from_date:
        parser.error("--to-date must not precede --from-date")
    maximum = min(max(args.max_records_per_window, 1), 500)
    page_size = min(max(args.page_size, 1), 250)
    provider = PlotaProvider(provider_api_key_from_secret(args.secret_arn))
    records: dict[str, dict[str, object]] = {}
    searches: list[dict[str, object]] = []
    for start, end in month_windows(args.from_date, args.to_date):
        for term in dict.fromkeys(args.term):
            count = 0
            query = PlanningQuery(
                from_date=start,
                to_date=end,
                search_term=term,
                max_records=maximum,
                page_size=page_size,
            )
            for record in provider.applications(query):
                count += 1
                value = asdict(record)
                value["application_date"] = (
                    record.application_date.isoformat() if record.application_date else None
                )
                value["decision_date"] = (
                    record.decision_date.isoformat() if record.decision_date else None
                )
                existing = records.setdefault(record.application_id, value)
                terms = set(existing.get("research_terms") or [])
                terms.add(term)
                existing["research_terms"] = sorted(terms)
            searches.append(
                {
                    "from_date": start.isoformat(),
                    "to_date": end.isoformat(),
                    "term": term,
                    "records": count,
                }
            )
    output = {
        "provider": "PLOTA",
        "retrieval_date": date.today().isoformat(),
        "bounded": True,
        "searches": searches,
        "records": sorted(records.values(), key=lambda item: str(item["application_id"])),
    }
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"searches": len(searches), "unique_records": len(records)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

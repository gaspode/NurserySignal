from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from app.verticals import CHILDRENS_HOME, NURSERY, validate_vertical

BACKFILL_VERSION = "planning-history-v1"
MAX_HISTORICAL_DAYS = 550
MAX_CHUNK_DAYS = 7
MAX_CHUNKS = 80
MAX_TOTAL_RECORDS = 60_000
MIN_TOTAL_RECORDS = 100


@dataclass(frozen=True)
class PlanningBackfillBounds:
    from_date: date
    to_date: date
    verticals: tuple[str, ...]
    total_record_cap: int
    chunk_days: int = MAX_CHUNK_DAYS

    @classmethod
    def from_values(
        cls,
        *,
        from_date: str,
        to_date: str,
        vertical: str = "ALL",
        total_record_cap: int = MAX_TOTAL_RECORDS,
        chunk_days: int = MAX_CHUNK_DAYS,
    ) -> PlanningBackfillBounds:
        start = date.fromisoformat(str(from_date))
        end = date.fromisoformat(str(to_date))
        if end < start:
            raise ValueError("to_date must not be before from_date")
        days = (end - start).days + 1
        if days > MAX_HISTORICAL_DAYS:
            raise ValueError(f"historical Planning range cannot exceed {MAX_HISTORICAL_DAYS} days")
        chunk_days = min(max(int(chunk_days), 1), MAX_CHUNK_DAYS)
        if math.ceil(days / chunk_days) > MAX_CHUNKS:
            raise ValueError(f"historical Planning backfill cannot exceed {MAX_CHUNKS} chunks")
        total_record_cap = min(max(int(total_record_cap), MIN_TOTAL_RECORDS), MAX_TOTAL_RECORDS)
        if vertical == "ALL":
            verticals = (NURSERY, CHILDRENS_HOME)
        else:
            value = validate_vertical(vertical)
            if value not in {NURSERY, CHILDRENS_HOME}:
                raise ValueError("historical Planning backfill supports active verticals only")
            verticals = (value,)
        return cls(start, end, verticals, total_record_cap, chunk_days)

    @property
    def chunks(self) -> tuple[tuple[date, date], ...]:
        result: list[tuple[date, date]] = []
        cursor = self.from_date
        while cursor <= self.to_date:
            end = min(cursor + timedelta(days=self.chunk_days - 1), self.to_date)
            result.append((cursor, end))
            cursor = end + timedelta(days=1)
        return tuple(result)

    def chunk_limits(self) -> tuple[int, int]:
        # Plota is queried once for nursery terms and twice for children's-home
        # variants. Allocate the aggregate cap evenly across chunks, then 2:1
        # between those query families. PlanningQuery applies its own hard caps.
        per_chunk = min(max(math.ceil(self.total_record_cap / len(self.chunks)), 3), 750)
        nursery = min(max(math.ceil(per_chunk * 2 / 3), 1), 500)
        care = min(max(per_chunk - nursery, 2), 250)
        return nursery, care

    def parameters(self) -> dict[str, Any]:
        return {
            "backfill_version": BACKFILL_VERSION,
            "from_date": self.from_date.isoformat(),
            "to_date": self.to_date.isoformat(),
            "verticals": list(self.verticals),
            "chunk_days": self.chunk_days,
            "chunks_total": len(self.chunks),
            "total_record_cap": self.total_record_cap,
        }


def chunk_payload(
    bounds: PlanningBackfillBounds,
    *,
    backfill_id: str,
    parent_started_at: str,
    chunk_index: int,
    cumulative_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    if not 0 <= chunk_index < len(bounds.chunks):
        raise ValueError("historical Planning chunk index is out of bounds")
    chunk_from, chunk_to = bounds.chunks[chunk_index]
    nursery_limit, care_limit = bounds.chunk_limits()
    return {
        "source": "historical_backfill",
        "invocation_source": "historical_backfill",
        "backfill_version": BACKFILL_VERSION,
        "backfill_id": backfill_id,
        "parent_started_at": parent_started_at,
        "requested_from_date": bounds.from_date.isoformat(),
        "requested_to_date": bounds.to_date.isoformat(),
        "from_date": chunk_from.isoformat(),
        "to_date": chunk_to.isoformat(),
        "chunk_days": bounds.chunk_days,
        "chunk_index": chunk_index,
        "chunks_total": len(bounds.chunks),
        "total_record_cap": bounds.total_record_cap,
        "max_records": nursery_limit,
        "care_max_records": care_limit,
        "page_size": min(nursery_limit, 100),
        "verticals": list(bounds.verticals),
        "cumulative_counts": cumulative_counts or {},
        "run_id": f"{backfill_id}-chunk-{chunk_index + 1:03d}",
        "run_started_at": parent_started_at,
    }


def bounds_from_chunk(payload: dict[str, Any]) -> PlanningBackfillBounds:
    if payload.get("backfill_version") != BACKFILL_VERSION:
        raise ValueError("unsupported historical Planning backfill version")
    verticals = payload.get("verticals")
    if not isinstance(verticals, list) or not verticals:
        raise ValueError("historical Planning backfill requires verticals")
    validated = tuple(dict.fromkeys(validate_vertical(str(item)) for item in verticals))
    if not set(validated).issubset({NURSERY, CHILDRENS_HOME}):
        raise ValueError("historical Planning backfill supports active verticals only")
    bounds = PlanningBackfillBounds(
        from_date=date.fromisoformat(str(payload["requested_from_date"])),
        to_date=date.fromisoformat(str(payload["requested_to_date"])),
        verticals=validated,
        total_record_cap=min(
            max(int(payload.get("total_record_cap", MAX_TOTAL_RECORDS)), MIN_TOTAL_RECORDS),
            MAX_TOTAL_RECORDS,
        ),
        chunk_days=min(max(int(payload.get("chunk_days", MAX_CHUNK_DAYS)), 1), MAX_CHUNK_DAYS),
    )
    # Re-run validation rather than trusting a queue message.
    return PlanningBackfillBounds.from_values(
        from_date=bounds.from_date.isoformat(),
        to_date=bounds.to_date.isoformat(),
        vertical="ALL" if set(validated) == {NURSERY, CHILDRENS_HOME} else validated[0],
        total_record_cap=bounds.total_record_cap,
        chunk_days=bounds.chunk_days,
    )


def add_counts(*values: dict[str, Any]) -> dict[str, int]:
    combined: dict[str, int] = {}
    for value in values:
        for key, count in value.items():
            if isinstance(count, bool) or not isinstance(count, (int, float)):
                continue
            combined[key] = combined.get(key, 0) + int(count)
    return combined

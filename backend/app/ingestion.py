from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class NormalizedSignal:
    """Common collector output persisted before classification or enrichment."""

    source_type: str
    source_url: str
    external_id: str | None
    discovered_at: datetime
    title: str
    raw_text: str
    location_hint: str | None = None
    organisation_hint: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> NormalizedSignal:
        required = ("source_type", "source_url", "discovered_at", "title", "raw_text")
        missing = [key for key in required if not payload.get(key)]
        if missing:
            raise ValueError(f"missing required signal fields: {', '.join(missing)}")

        parsed_url = urlparse(str(payload["source_url"]))
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("source_url must be an absolute HTTP(S) URL")

        discovered_at = payload["discovered_at"]
        if isinstance(discovered_at, str):
            discovered_at = datetime.fromisoformat(discovered_at.replace("Z", "+00:00"))
        if not isinstance(discovered_at, datetime):
            raise ValueError("discovered_at must be an ISO timestamp")
        if discovered_at.tzinfo is None:
            discovered_at = discovered_at.replace(tzinfo=UTC)

        metadata = payload.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be an object")

        return cls(
            source_type=str(payload["source_type"]),
            source_url=str(payload["source_url"]),
            external_id=str(payload["external_id"]) if payload.get("external_id") else None,
            discovered_at=discovered_at.astimezone(UTC),
            title=str(payload["title"]),
            raw_text=str(payload["raw_text"]),
            location_hint=str(payload["location_hint"]) if payload.get("location_hint") else None,
            organisation_hint=(
                str(payload["organisation_hint"]) if payload.get("organisation_hint") else None
            ),
            metadata=metadata,
        )


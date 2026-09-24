from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

CURRENT_SIGNAL_SCHEMA_VERSION = "1.0"
SUPPORTED_SIGNAL_SCHEMA_VERSIONS = {CURRENT_SIGNAL_SCHEMA_VERSION}


@dataclass(frozen=True)
class NormalizedSignal:
    """Common collector output persisted before classification or enrichment."""

    schema_version: str
    source_type: str
    source_url: str
    external_id: str
    discovered_at: datetime
    title: str
    raw_text: str
    location_hint: str | None = None
    organisation_hint: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> NormalizedSignal:
        required = (
            "source_type",
            "source_url",
            "external_id",
            "discovered_at",
            "title",
            "raw_text",
        )
        missing = [key for key in required if not payload.get(key)]
        if missing:
            raise ValueError(f"missing required signal fields: {', '.join(missing)}")

        schema_version = str(payload.get("schema_version", CURRENT_SIGNAL_SCHEMA_VERSION))
        if schema_version not in SUPPORTED_SIGNAL_SCHEMA_VERSIONS:
            raise ValueError(f"unsupported schema_version: {schema_version}")

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
            schema_version=schema_version,
            source_type=str(payload["source_type"]),
            source_url=str(payload["source_url"]),
            external_id=str(payload["external_id"]),
            discovered_at=discovered_at.astimezone(UTC),
            title=str(payload["title"]),
            raw_text=str(payload["raw_text"]),
            location_hint=str(payload["location_hint"]) if payload.get("location_hint") else None,
            organisation_hint=(
                str(payload["organisation_hint"]) if payload.get("organisation_hint") else None
            ),
            metadata=metadata,
        )

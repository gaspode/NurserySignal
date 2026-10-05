"""Role-safe, provider-backed Planning site identity extraction."""

from __future__ import annotations

from typing import Any

from app.correlation import normalize_identity
from app.planning import planning_record_from_signal
from app.planning_families import planning_authority, primary_planning_reference

PLANNING_SITE_IDENTITY_VERSION = "planning-site-identity-v1"


def _value(value: Any, path: str) -> dict[str, Any] | None:
    text = " ".join(str(value or "").split()).strip(" ,")
    return {"value": text, "source_path": path, "deterministic": True} if text else None


def extract_planning_site_identity(raw: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Use only labelled Planning address/location/reference fields.

    Applicant/agent provenance intentionally belongs in planning_party_provenance;
    it is never made an operator through this site extractor.
    """
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    provider = metadata.get("provider_record")
    if provider is not None and not isinstance(provider, dict):
        return None, "INCOMPLETE"
    try:
        record = planning_record_from_signal(raw)
    except (KeyError, TypeError, ValueError):
        return None, "INCOMPLETE"
    provider = provider if isinstance(provider, dict) else {}
    location = provider.get("location") if isinstance(provider.get("location"), dict) else {}
    site_name = _value(location.get("name") or provider.get("site_name"), "provider.location.name")
    address = _value(record.address, "provider.address")
    postcode = _value(record.postcode, "provider.postcode")
    town = _value(location.get("town") or location.get("locality"), "provider.location.town")
    authority = _value(planning_authority(metadata), "metadata.council")
    reference = _value(
        primary_planning_reference(str(raw.get("external_id") or ""), metadata),
        "metadata.planning_reference",
    )
    coordinates = None
    if record.latitude is not None and record.longitude is not None:
        coordinates = {
            "latitude": record.latitude,
            "longitude": record.longitude,
            "source_path": "provider.location.lat/lng",
            "deterministic": True,
        }
    identity = {
        "version": PLANNING_SITE_IDENTITY_VERSION,
        "site_name": site_name,
        "site_address": address,
        "normalized_site_address": normalize_identity(record.address) if record.address else None,
        "site_postcode": postcode,
        "site_town": town,
        "coordinates": coordinates,
        "planning_authority": authority,
        "planning_reference": reference,
        "source_application_id": _value(record.application_id, "provider.id"),
        "source_signal_id": str(raw.get("id") or ""),
    }
    if address and postcode:
        outcome = "FULL_SITE_IDENTITY"
    elif address:
        outcome = "ADDRESS_AND_POSTCODE" if postcode else "INCOMPLETE"
    elif postcode:
        outcome = "POSTCODE_ONLY"
    elif site_name:
        outcome = "SITE_NAME_ONLY"
    elif coordinates:
        outcome = "COORDINATES_ONLY"
    else:
        outcome = "INCOMPLETE"
    identity["outcome"] = outcome
    return identity, outcome

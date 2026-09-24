from __future__ import annotations

import hashlib
import re
from datetime import UTC
from typing import Any

import boto3

from app.config import Settings
from app.ingestion import NormalizedSignal


class EvidencePersistenceError(RuntimeError):
    """Raised when raw evidence cannot be persisted."""


def evidence_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def evidence_key(signal: NormalizedSignal, payload: bytes) -> str:
    identity = f"{signal.source_type}\x00{signal.external_id}".encode()
    identity_hash = hashlib.sha256(identity).hexdigest()
    source = re.sub(r"[^a-z0-9-]+", "-", signal.source_type.lower()).strip("-") or "unknown"
    discovered = signal.discovered_at.astimezone(UTC).date().isoformat()
    return f"signals/{source}/{discovered}/{identity_hash}/raw.json"


def evidence_revision_key(signal: NormalizedSignal, payload: bytes) -> str:
    """Use a separate immutable object for a changed provider record."""
    base = evidence_key(signal, payload).removesuffix("/raw.json")
    return f"{base}/revisions/{evidence_sha256(payload)}.json"


def put_raw_evidence(settings: Settings, bucket: str, key: str, payload: bytes) -> None:
    try:
        boto3.client("s3").put_object(
            Bucket=bucket,
            Key=key,
            Body=payload,
            ContentType="application/json",
            ServerSideEncryption="AES256",
            Metadata={"signal-schema-version": "1.0"},
        )
    except Exception as exc:
        raise EvidencePersistenceError("raw evidence persistence failed") from exc


def evidence_reference(bucket: str, key: str, payload: bytes) -> dict[str, Any]:
    return {
        "bucket": bucket,
        "key": key,
        "sha256": evidence_sha256(payload),
    }


def presigned_evidence_url(bucket: str, key: str, expires_in: int = 300) -> str:
    """Create a short-lived authenticated download URL for an evidence object."""
    try:
        return boto3.client("s3").generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires_in,
        )
    except Exception as exc:
        raise EvidencePersistenceError("evidence URL generation failed") from exc

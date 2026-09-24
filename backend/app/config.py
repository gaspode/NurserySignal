from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    service_name: str = "nurserysignal-api"
    environment: str = "local"
    database_url: str | None = None
    db_secret_arn: str | None = None
    evidence_bucket: str | None = None
    enrichment_queue_url: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            service_name=os.getenv("SERVICE_NAME", "nurserysignal-api"),
            environment=os.getenv("APP_ENV", "local"),
            database_url=os.getenv("DATABASE_URL") or None,
            db_secret_arn=os.getenv("DB_SECRET_ARN") or None,
            evidence_bucket=os.getenv("EVIDENCE_BUCKET") or None,
            enrichment_queue_url=os.getenv("ENRICHMENT_QUEUE_URL") or None,
        )

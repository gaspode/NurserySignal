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
    ingestion_queue_url: str | None = None
    enrichment_queue_url: str | None = None
    planning_provider_secret_arn: str | None = None
    planning_provider_base_url: str = "https://api.plota.co.uk/v1"
    recruitment_provider_secret_arn: str | None = None
    recruitment_provider_base_url: str = "https://api.apprenticeships.education.gov.uk/vacancies"
    admin_group: str = "NurserySignalAdmins"
    ai_shadow_enabled: bool = False
    ai_model_id: str = "eu.amazon.nova-lite-v1:0"
    ai_prompt_version: str = "shadow-v3"
    ai_planning_prompt_version: str = "planning-shadow-v2"
    ai_recruitment_prompt_version: str = "recruitment-shadow-v3"
    ai_care_planning_prompt_version: str = "care-planning-shadow-v1"
    ai_care_recruitment_prompt_version: str = "care-recruitment-shadow-v1"
    source_runs_table_name: str | None = None
    planning_manual_run_queue_url: str | None = None
    recruitment_manual_run_queue_url: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            service_name=os.getenv("SERVICE_NAME", "nurserysignal-api"),
            environment=os.getenv("APP_ENV", "local"),
            database_url=os.getenv("DATABASE_URL") or None,
            db_secret_arn=os.getenv("DB_SECRET_ARN") or None,
            evidence_bucket=os.getenv("EVIDENCE_BUCKET") or None,
            ingestion_queue_url=os.getenv("INGESTION_QUEUE_URL") or None,
            enrichment_queue_url=os.getenv("ENRICHMENT_QUEUE_URL") or None,
            planning_provider_secret_arn=os.getenv("PLANNING_PROVIDER_SECRET_ARN") or None,
            planning_provider_base_url=os.getenv(
                "PLANNING_PROVIDER_BASE_URL", "https://api.plota.co.uk/v1"
            ),
            recruitment_provider_secret_arn=os.getenv("RECRUITMENT_PROVIDER_SECRET_ARN") or None,
            recruitment_provider_base_url=os.getenv(
                "RECRUITMENT_PROVIDER_BASE_URL",
                "https://api.apprenticeships.education.gov.uk/vacancies",
            ),
            admin_group=os.getenv("ADMIN_GROUP", "NurserySignalAdmins"),
            ai_shadow_enabled=os.getenv("AI_SHADOW_ENABLED", "false").lower()
            in {"1", "true", "yes"},
            ai_model_id=os.getenv("AI_MODEL_ID", "eu.amazon.nova-lite-v1:0"),
            ai_prompt_version=os.getenv("AI_PROMPT_VERSION", "shadow-v3"),
            ai_planning_prompt_version=os.getenv(
                "AI_PLANNING_PROMPT_VERSION", "planning-shadow-v2"
            ),
            ai_recruitment_prompt_version=os.getenv(
                "AI_RECRUITMENT_PROMPT_VERSION", "recruitment-shadow-v3"
            ),
            ai_care_planning_prompt_version=os.getenv(
                "AI_CARE_PLANNING_PROMPT_VERSION", "care-planning-shadow-v1"
            ),
            ai_care_recruitment_prompt_version=os.getenv(
                "AI_CARE_RECRUITMENT_PROMPT_VERSION", "care-recruitment-shadow-v1"
            ),
            source_runs_table_name=os.getenv("SOURCE_RUNS_TABLE_NAME") or None,
            planning_manual_run_queue_url=os.getenv("PLANNING_MANUAL_RUN_QUEUE_URL") or None,
            recruitment_manual_run_queue_url=os.getenv("RECRUITMENT_MANUAL_RUN_QUEUE_URL") or None,
        )

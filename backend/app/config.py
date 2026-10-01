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
    ofsted_data_url: str = (
        "https://assets.publishing.service.gov.uk/media/697345cb51bd707cb10ed934/"
        "Inspection_and_regulation_of_childrens_social_care_and_supported_"
        "accommodation_providers_2025.ods"
    )
    companies_house_secret_arn: str | None = None
    companies_house_base_url: str = "https://api.company-information.service.gov.uk"
    admin_group: str = "NurserySignalAdmins"
    customer_group: str = "CareSignalCustomers"
    cognito_user_pool_id: str | None = None
    caresignal_email_from: str | None = None
    caresignal_portal_url: str | None = None
    customer_digest_queue_url: str | None = None
    customer_provisioning_queue_url: str | None = None
    ai_shadow_enabled: bool = False
    ai_model_id: str = "eu.amazon.nova-lite-v1:0"
    ai_prompt_version: str = "shadow-v3"
    ai_planning_prompt_version: str = "planning-shadow-v3"
    ai_recruitment_prompt_version: str = "recruitment-shadow-v3"
    ai_care_planning_prompt_version: str = "care-planning-shadow-v2"
    ai_care_recruitment_prompt_version: str = "care-recruitment-shadow-v1"
    source_runs_table_name: str | None = None
    planning_manual_run_queue_url: str | None = None
    recruitment_manual_run_queue_url: str | None = None
    ofsted_manual_run_queue_url: str | None = None
    companies_house_manual_run_queue_url: str | None = None
    companies_house_lookup_function_name: str | None = None
    procurement_manual_run_queue_url: str | None = None
    care_lifecycle_watcher_schedule_enabled: bool = False
    mcp_resource_url: str | None = None
    mcp_oauth_issuer: str | None = None
    mcp_oauth_authorization_server: str | None = None
    mcp_token_issuer: str | None = None
    mcp_token_jwks: str | None = None
    mcp_oauth_callback_url: str | None = None
    mcp_oauth_transactions_table_name: str | None = None
    mcp_user_client_id: str | None = None
    mcp_service_client_id: str | None = None
    mcp_rate_limit_per_minute: int = 60

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
            ofsted_data_url=os.getenv(
                "OFSTED_DATA_URL",
                "https://assets.publishing.service.gov.uk/media/697345cb51bd707cb10ed934/"
                "Inspection_and_regulation_of_childrens_social_care_and_supported_"
                "accommodation_providers_2025.ods",
            ),
            companies_house_secret_arn=os.getenv("COMPANIES_HOUSE_SECRET_ARN") or None,
            companies_house_base_url=os.getenv(
                "COMPANIES_HOUSE_BASE_URL",
                "https://api.company-information.service.gov.uk",
            ),
            admin_group=os.getenv("ADMIN_GROUP", "NurserySignalAdmins"),
            customer_group=os.getenv("CUSTOMER_GROUP", "CareSignalCustomers"),
            cognito_user_pool_id=os.getenv("COGNITO_USER_POOL_ID") or None,
            caresignal_email_from=os.getenv("CARESIGNAL_EMAIL_FROM") or None,
            caresignal_portal_url=os.getenv("CARESIGNAL_PORTAL_URL") or None,
            customer_digest_queue_url=os.getenv("CUSTOMER_DIGEST_QUEUE_URL") or None,
            customer_provisioning_queue_url=(os.getenv("CUSTOMER_PROVISIONING_QUEUE_URL") or None),
            ai_shadow_enabled=os.getenv("AI_SHADOW_ENABLED", "false").lower()
            in {"1", "true", "yes"},
            ai_model_id=os.getenv("AI_MODEL_ID", "eu.amazon.nova-lite-v1:0"),
            ai_prompt_version=os.getenv("AI_PROMPT_VERSION", "shadow-v3"),
            ai_planning_prompt_version=os.getenv(
                "AI_PLANNING_PROMPT_VERSION", "planning-shadow-v3"
            ),
            ai_recruitment_prompt_version=os.getenv(
                "AI_RECRUITMENT_PROMPT_VERSION", "recruitment-shadow-v3"
            ),
            ai_care_planning_prompt_version=os.getenv(
                "AI_CARE_PLANNING_PROMPT_VERSION", "care-planning-shadow-v2"
            ),
            ai_care_recruitment_prompt_version=os.getenv(
                "AI_CARE_RECRUITMENT_PROMPT_VERSION", "care-recruitment-shadow-v1"
            ),
            source_runs_table_name=os.getenv("SOURCE_RUNS_TABLE_NAME") or None,
            planning_manual_run_queue_url=os.getenv("PLANNING_MANUAL_RUN_QUEUE_URL") or None,
            recruitment_manual_run_queue_url=os.getenv("RECRUITMENT_MANUAL_RUN_QUEUE_URL") or None,
            ofsted_manual_run_queue_url=os.getenv("OFSTED_MANUAL_RUN_QUEUE_URL") or None,
            companies_house_manual_run_queue_url=(
                os.getenv("COMPANIES_HOUSE_MANUAL_RUN_QUEUE_URL") or None
            ),
            companies_house_lookup_function_name=(
                os.getenv("COMPANIES_HOUSE_LOOKUP_FUNCTION_NAME") or None
            ),
            procurement_manual_run_queue_url=(
                os.getenv("PROCUREMENT_MANUAL_RUN_QUEUE_URL") or None
            ),
            care_lifecycle_watcher_schedule_enabled=os.getenv(
                "CARE_LIFECYCLE_WATCHER_SCHEDULE_ENABLED", "false"
            ).lower()
            in {"1", "true", "yes"},
            mcp_resource_url=os.getenv("MCP_RESOURCE_URL") or None,
            mcp_oauth_issuer=os.getenv("MCP_OAUTH_ISSUER") or None,
            mcp_oauth_authorization_server=(os.getenv("MCP_OAUTH_AUTHORIZATION_SERVER") or None),
            mcp_token_issuer=os.getenv("MCP_TOKEN_ISSUER") or None,
            mcp_token_jwks=os.getenv("MCP_TOKEN_JWKS") or None,
            mcp_oauth_callback_url=os.getenv("MCP_OAUTH_CALLBACK_URL") or None,
            mcp_oauth_transactions_table_name=(
                os.getenv("MCP_OAUTH_TRANSACTIONS_TABLE_NAME") or None
            ),
            mcp_user_client_id=os.getenv("MCP_USER_CLIENT_ID") or None,
            mcp_service_client_id=os.getenv("MCP_SERVICE_CLIENT_ID") or None,
            mcp_rate_limit_per_minute=max(1, int(os.getenv("MCP_RATE_LIMIT_PER_MINUTE", "60"))),
        )

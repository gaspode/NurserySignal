from pathlib import Path

from app.migrations import migration_files


def test_initial_migration_exists_and_contains_provenance_tables() -> None:
    files = migration_files()
    assert [path.name for path in files] == [
        "0001_initial.sql",
        "0002_ingestion_vertical_slice.sql",
        "0003_planning_revisions.sql",
        "0004_planning_reprocess_audit.sql",
        "0005_ai_shadow_reviews.sql",
        "0006_opportunity_correlation_v1.sql",
        "0007_recruitment_ai_shadow_v2.sql",
        "0008_recruitment_ai_shadow_v3.sql",
        "0009_source_aware_ai_shadow.sql",
        "0010_opportunity_matching_corrections.sql",
        "0011_opportunity_creation_policy.sql",
        "0012_opportunity_reason_separation.sql",
        "0013_canonical_match_review_cleanup.sql",
        "0014_signalhub_verticals.sql",
        "0015_caresignal_activation.sql",
        "0016_regulatory_and_organisation_enrichment.sql",
        "0017_organisation_review_evidence.sql",
        "0018_ofsted_urn_enrichment.sql",
        "0019_historical_backtesting.sql",
        "0020_historical_research_corpus.sql",
        "0021_backtest_failed_retry.sql",
        "0022_caresignal_customer_mvp.sql",
        "0023_organisation_review_ofsted_enrichment.sql",
        "0024_customer_access_requests.sql",
        "0025_public_authority_organisations.sql",
        "0026_planning_application_families.sql",
        "0027_care_opportunity_lifecycle_v1.sql",
        "0028_care_planning_watcher_v2.sql",
        "0029_care_planning_watcher_runtime.sql",
    ]
    sql = "\n".join(path.read_text(encoding="utf-8") for path in files)
    tables = (
        "operators",
        "nurseries",
        "opportunities",
        "raw_signals",
        "source_documents",
        "opportunity_signals",
    )
    for table in tables:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "provenance JSONB" in sql
    assert "CREATE TABLE IF NOT EXISTS signal_enrichments" in sql
    assert "review_status IN ('PENDING', 'APPROVED', 'REJECTED')" in sql
    assert "CREATE TABLE IF NOT EXISTS raw_signal_revisions" in sql
    assert "CREATE TABLE IF NOT EXISTS admin_audit_events" in sql
    assert "CREATE TABLE IF NOT EXISTS signal_ai_reviews" in sql
    assert "UNIQUE (raw_signal_id, provider, model_id, prompt_version)" in sql
    assert "commercial_change_evidence" in sql
    assert "recruitment_relevance" in sql
    assert "planning_relevance" in sql
    assert "CREATE TABLE IF NOT EXISTS opportunity_match_reviews" in sql
    assert "CREATE TABLE IF NOT EXISTS opportunity_signal_history" in sql
    assert "opportunities_change_type_check" in sql
    assert "match_outcome" in sql
    assert "creation_reason" in sql
    assert "SUPERSEDED" in sql
    assert "signalhub_check_opportunity_signal_vertical" in sql
    assert "supported_verticals" in sql
    assert "candidate_fingerprint" in sql
    assert "CREATE TABLE IF NOT EXISTS customer_accounts" in sql
    assert "CREATE TABLE IF NOT EXISTS customer_users" in sql
    assert "CREATE TABLE IF NOT EXISTS customer_saved_opportunities" in sql
    assert "CREATE TABLE IF NOT EXISTS ofsted_urn_enrichments" in sql
    assert "provider_registered_address" in sql
    assert "CREATE TABLE IF NOT EXISTS benchmark_cases" in sql
    assert "CREATE TABLE IF NOT EXISTS backtest_runs" in sql
    assert "CREATE TABLE IF NOT EXISTS backtest_case_results" in sql
    assert "CREATE OR REPLACE VIEW backtest_labelled_decisions" in sql
    assert "truth_role', 'OUTCOME_ONLY'" in sql
    assert "'ofsted:' || (rs.metadata->>'ofsted_urn')" in sql
    assert "CREATE TABLE IF NOT EXISTS historical_research_records" in sql
    assert "CREATE TABLE IF NOT EXISTS benchmark_case_research" in sql
    assert "backtest_runs_active_fingerprint_idx" in sql
    assert "CREATE TABLE IF NOT EXISTS customer_access_requests" in sql
    assert "organisation_type IN ('PRIVATE_COMPANY', 'PUBLIC_AUTHORITY', 'UNKNOWN', 'OTHER')" in sql
    assert "CREATE TABLE IF NOT EXISTS opportunity_lifecycle_history" in sql
    assert "CREATE TABLE IF NOT EXISTS planning_lifecycle_watches" in sql
    assert "publication_automation_blocked" in sql
    assert "care-planning-watcher-v2" in sql


def test_ai_review_insert_has_one_value_placeholder_per_column() -> None:
    repository = Path("backend/app/repository.py").read_text()
    statement = repository.split("def save_ai_review", 1)[1].split("ON CONFLICT", 1)[0]
    assert statement.count("%s") == 18

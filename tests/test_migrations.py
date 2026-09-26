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


def test_ai_review_insert_has_one_value_placeholder_per_column() -> None:
    repository = Path("backend/app/repository.py").read_text()
    statement = repository.split("def save_ai_review", 1)[1].split("ON CONFLICT", 1)[0]
    assert statement.count("%s") == 17

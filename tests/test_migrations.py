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

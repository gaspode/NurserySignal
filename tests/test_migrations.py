from app.migrations import migration_files


def test_initial_migration_exists_and_contains_provenance_tables() -> None:
    files = migration_files()
    assert [path.name for path in files] == [
        "0001_initial.sql",
        "0002_ingestion_vertical_slice.sql",
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

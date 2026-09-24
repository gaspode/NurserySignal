from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import psycopg

from app.config import Settings


def migration_files() -> list[Path]:
    return sorted(Path(__file__).with_name("sql").glob("[0-9][0-9][0-9][0-9]_*.sql"))


def apply_migrations(database_url: str) -> list[str]:
    applied: list[str] = []
    with psycopg.connect(database_url, connect_timeout=10) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version VARCHAR(255) PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        existing = {row[0] for row in rows}
        for migration in migration_files():
            if migration.name in existing:
                continue
            conn.execute(migration.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (migration.name,))
            applied.append(migration.name)
        conn.commit()
    return applied


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    settings = Settings.from_env()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is not configured")
    return {"applied": apply_migrations(settings.database_url)}


if __name__ == "__main__":
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    print({"applied": apply_migrations(database_url)})


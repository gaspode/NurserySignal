from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg

from app.config import Settings
from app.secrets import database_url_from_secret


def resolve_database_url(settings: Settings) -> str:
    if settings.database_url:
        return settings.database_url
    if settings.db_secret_arn:
        return database_url_from_secret(settings.db_secret_arn)
    raise RuntimeError("DATABASE_URL or DB_SECRET_ARN is not configured")


@contextmanager
def connection(settings: Settings) -> Iterator[psycopg.Connection]:
    with psycopg.connect(resolve_database_url(settings), connect_timeout=5) as conn:
        yield conn


def check_connection(settings: Settings) -> bool:
    try:
        with connection(settings) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg

from app.config import Settings


@contextmanager
def connection(settings: Settings) -> Iterator[psycopg.Connection]:
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is not configured")
    with psycopg.connect(settings.database_url, connect_timeout=5) as conn:
        yield conn


def check_connection(settings: Settings) -> bool:
    try:
        with connection(settings) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


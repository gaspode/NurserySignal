from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

import boto3
import psycopg


@lru_cache(maxsize=4)
def database_url_from_secret(secret_arn: str) -> str:
    """Resolve PostgreSQL connection details without logging the secret payload."""
    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_arn)
    secret_string = response.get("SecretString")
    if not secret_string:
        raise RuntimeError("database secret has no SecretString")
    try:
        values: dict[str, Any] = json.loads(secret_string)
        return psycopg.conninfo.make_conninfo(
            host=values["host"],
            port=values["port"],
            dbname=values["dbname"],
            user=values["username"],
            password=values["password"],
            sslmode="require",
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("database secret is missing required fields") from exc


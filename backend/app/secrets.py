from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

import boto3
import psycopg


@lru_cache(maxsize=4)
def secret_string(secret_arn: str) -> str:
    """Resolve a Secrets Manager string without logging its payload."""
    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_arn)
    secret_string = response.get("SecretString")
    if not secret_string:
        raise RuntimeError("secret has no SecretString")
    return str(secret_string)


def provider_api_key_from_secret(secret_arn: str) -> str:
    value = secret_string(secret_arn)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    if isinstance(parsed, dict) and parsed.get("api_key"):
        return str(parsed["api_key"])
    raise RuntimeError("provider secret must contain an api_key")


@lru_cache(maxsize=4)
def database_url_from_secret(secret_arn: str) -> str:
    """Resolve PostgreSQL connection details without logging the secret payload."""
    try:
        values: dict[str, Any] = json.loads(secret_string(secret_arn))
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

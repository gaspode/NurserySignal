import json

from app.secrets import database_url_from_secret


class FakeSecretsManager:
    def get_secret_value(self, *, SecretId: str) -> dict[str, str]:
        assert SecretId == "arn:aws:secretsmanager:eu-west-1:123:secret:test"
        return {
            "SecretString": json.dumps({
                "host": "db.example",
                "port": 5432,
                "dbname": "nurserysignal",
                "username": "app-user",
                "password": "p@ss word",
            })
        }


def test_database_secret_is_converted_to_safe_conninfo(monkeypatch) -> None:
    monkeypatch.setattr("app.secrets.boto3.client", lambda service: FakeSecretsManager())
    database_url_from_secret.cache_clear()

    conninfo = database_url_from_secret("arn:aws:secretsmanager:eu-west-1:123:secret:test")

    assert "host=db.example" in conninfo
    assert "dbname=nurserysignal" in conninfo
    assert "password='p@ss word'" in conninfo
    assert "SecretString" not in conninfo


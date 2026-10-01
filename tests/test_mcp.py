import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from app import mcp
from app.config import Settings
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

REAL_DECODE_ACCESS_TOKEN = mcp._decode_access_token
PKCE_VALUE = "A" * 43


def event(body=None, *, authorized=True, method="POST", path="/mcp"):
    value = {
        "rawPath": path,
        "headers": {"authorization": "Bearer test-token"} if authorized else {},
        "requestContext": {"http": {"method": method}},
    }
    if body is not None:
        value["body"] = json.dumps(body)
    return value


@pytest.fixture(autouse=True)
def auth(monkeypatch):
    monkeypatch.setenv("MCP_SERVICE_CLIENT_ID", "service-client")
    monkeypatch.setenv("MCP_USER_CLIENT_ID", "user-client")
    monkeypatch.setenv("MCP_RESOURCE_URL", "https://api.example/mcp")
    monkeypatch.setenv("MCP_OAUTH_ISSUER", "https://api.example")
    monkeypatch.setenv("MCP_OAUTH_AUTHORIZATION_SERVER", "https://login.example")
    monkeypatch.setenv("MCP_TOKEN_ISSUER", "https://tokens.example")
    monkeypatch.setenv("MCP_OAUTH_CALLBACK_URL", "https://api.example/oauth/callback")
    monkeypatch.setenv("MCP_OAUTH_TRANSACTIONS_TABLE_NAME", "oauth-transactions")
    monkeypatch.setattr(
        mcp,
        "_decode_access_token",
        lambda token, settings: {
            "client_id": "service-client",
            "sub": "test-service",
            "scope": mcp.READ_SCOPE,
            "token_use": "access",
        },
    )


def test_authentication_and_read_scope_are_required(monkeypatch):
    assert mcp.handler(event({}, authorized=False), None)["statusCode"] == 401
    monkeypatch.setattr(
        mcp,
        "_decode_access_token",
        lambda token, settings: {
            "client_id": "service-client",
            "scope": "openid",
            "token_use": "access",
        },
    )
    assert mcp.handler(event({}), None)["statusCode"] == 401
    monkeypatch.setattr(
        mcp,
        "_decode_access_token",
        lambda token, settings: {
            "client_id": "wrong",
            "scope": mcp.READ_SCOPE,
            "token_use": "access",
        },
    )
    assert mcp.handler(event({}), None)["statusCode"] == 401


def test_admin_user_requires_admin_group(monkeypatch):
    request = event({})
    claims = {
        "client_id": "user-client",
        "sub": "admin-user",
        "scope": mcp.READ_SCOPE,
        "token_use": "access",
        "cognito:groups": "NurserySignalAdmins",
    }
    monkeypatch.setattr(mcp, "_decode_access_token", lambda token, settings: claims)
    assert mcp._authenticate(request, mcp.Settings.from_env())["auth_kind"] == "admin_user"
    claims["cognito:groups"] = "CareSignalCustomers"
    with pytest.raises(mcp.MCPError, match="admin/service"):
        mcp._authenticate(request, mcp.Settings.from_env())


def test_initialize_capabilities_and_stable_read_only_tool_catalogue():
    response = mcp.handler(
        event({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}), None
    )
    result = json.loads(response["body"])["result"]
    assert result["protocolVersion"] == mcp.PROTOCOL_VERSION
    listed = mcp.handler(event({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}), None)
    names = [item["name"] for item in json.loads(listed["body"])["result"]["tools"]]
    assert names == [item["name"] for item in mcp.TOOLS]
    assert names == list(mcp.HANDLERS)
    assert not any(
        word in name
        for name in names
        for word in ("publish", "withdraw", "approve", "reject", "trigger", "link")
    )
    assert all(item["annotations"]["readOnlyHint"] for item in mcp.TOOLS)
    assert all(
        item["securitySchemes"] == [{"type": "oauth2", "scopes": [mcp.READ_SCOPE]}]
        for item in mcp.TOOLS
    )


def test_capability_endpoint_is_authenticated_and_explicitly_read_only():
    denied = mcp.handler(event(authorized=False, method="GET", path="/mcp/capabilities"), None)
    assert denied["statusCode"] == 401
    response = mcp.handler(event(method="GET", path="/mcp/capabilities"), None)
    body = json.loads(response["body"])
    assert body["server_version"] == "signalhub-mcp-v1"
    assert body["schema_version"] == "signalhub-mcp-tools-v1"
    assert body["read_only"] is True
    assert body["required_scope"] == "signalhub-mcp/read"


def test_oauth_protected_resource_metadata_is_public_and_scoped():
    response = mcp.handler(
        event(authorized=False, method="GET", path="/.well-known/oauth-protected-resource/mcp"),
        None,
    )
    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert body["resource"] == "https://api.example/mcp"
    assert body["authorization_servers"] == ["https://api.example"]
    assert body["scopes_supported"] == ["signalhub-mcp/read"]


def test_unauthenticated_mcp_returns_discoverable_oauth_challenge():
    response = mcp.handler(event(authorized=False), None)
    assert response["statusCode"] == 401
    challenge = response["headers"]["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert (
        'resource_metadata="https://api.example/.well-known/oauth-protected-resource"' in challenge
    )
    assert 'scope="signalhub-mcp/read"' in challenge
    assert 'error="invalid_token"' in challenge


def test_oauth_server_metadata_advertises_cognito_pkce_facade():
    response = mcp.handler(
        event(authorized=False, method="GET", path="/.well-known/oauth-authorization-server"),
        None,
    )
    body = json.loads(response["body"])
    assert body["issuer"] == "https://api.example"
    assert body["authorization_endpoint"] == "https://api.example/oauth/authorize"
    assert body["token_endpoint"] == "https://api.example/oauth/token"
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert body["scopes_supported"] == [mcp.READ_SCOPE]
    assert body["token_endpoint_auth_methods_supported"] == ["none", "private_key_jwt"]
    assert body["token_endpoint_auth_signing_alg_values_supported"] == ["RS256"]
    assert body["client_id_metadata_document_supported"] is True
    assert body["authorization_response_iss_parameter_supported"] is True
    resource = mcp.protected_resource_metadata(mcp.Settings.from_env())
    assert body["issuer"] == resource["authorization_servers"][0]

    oidc = mcp.handler(
        event(authorized=False, method="GET", path="/.well-known/openid-configuration"), None
    )
    assert oidc["statusCode"] == 404
    assert json.loads(oidc["body"])["error"] == "not_supported"


def test_chatgpt_cimd_registration_is_pinned_without_runtime_network(monkeypatch):
    monkeypatch.setattr(
        mcp.urllib.request,
        "urlopen",
        lambda *args, **kwargs: pytest.fail("CIMD validation must not require runtime egress"),
    )
    metadata = mcp._fetch_chatgpt_cimd()
    assert metadata["client_id"] == mcp.CHATGPT_CIMD_URL
    assert metadata["redirect_uris"] == [mcp.CHATGPT_REDIRECT_URI]
    assert "authorization_code" in metadata["grant_types"]
    assert metadata["token_endpoint_auth_method"] == "private_key_jwt"
    assert metadata["jwks_uri"] == "https://chatgpt.com/oauth/jwks.json"


def test_codex_cimd_registration_is_pinned_without_runtime_network(monkeypatch):
    monkeypatch.setattr(
        mcp.urllib.request,
        "urlopen",
        lambda *args, **kwargs: pytest.fail("Codex CIMD must not require runtime egress"),
    )
    metadata = mcp._fetch_chatgpt_cimd(mcp.CODEX_CIMD_URL)
    assert metadata["client_id"] == mcp.CODEX_CIMD_URL
    assert metadata["token_endpoint_auth_method"] == "none"


def test_callback_specific_chatgpt_cimd_is_verified(monkeypatch):
    client_metadata_id = "client_abc-123"
    redirect_callback_id = "callback_xyz-789"
    client_id = f"https://chatgpt.com/oauth/{client_metadata_id}/client.json"
    redirect_uri = f"https://chatgpt.com/connector/oauth/{redirect_callback_id}"

    class Headers:
        @staticmethod
        def get_content_type():
            return "application/json"

    class Response:
        headers = Headers()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        @staticmethod
        def read(limit):
            assert limit == 65_537
            return json.dumps(
                {
                    "redirect_uris": [redirect_uri],
                }
            ).encode()

    def urlopen(request, timeout):
        assert request.full_url == client_id
        assert timeout == 5
        return Response()

    monkeypatch.setattr(mcp.urllib.request, "urlopen", urlopen)
    metadata = mcp._validate_chatgpt_client(client_id, redirect_uri)
    assert metadata["redirect_uris"] == [redirect_uri]


def test_callback_specific_cimd_rejects_untrusted_client_without_fetch(monkeypatch):
    monkeypatch.setattr(
        mcp.urllib.request,
        "urlopen",
        lambda *args, **kwargs: pytest.fail("untrusted client must not be fetched"),
    )
    with pytest.raises(mcp.MCPError, match="Unsupported OAuth client"):
        mcp._validate_chatgpt_client(
            "https://attacker.example/oauth/connection/client.json",
            "https://chatgpt.com/connector/oauth/connection",
        )


def test_codex_cimd_allows_rfc8252_ephemeral_loopback_port(monkeypatch):
    client_id = mcp.CODEX_CIMD_URL
    monkeypatch.setattr(
        mcp,
        "_fetch_chatgpt_cimd",
        lambda requested_client: {
            "client_id": requested_client,
            "redirect_uris": [
                "http://127.0.0.1/callback",
                "http://localhost/callback",
            ],
        },
    )
    metadata = mcp._validate_chatgpt_client(
        client_id, "http://127.0.0.1:57117/callback"
    )
    assert metadata["client_id"] == client_id


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "http://attacker.example:57117/callback",
        "http://127.0.0.1:57117/not-callback",
        "https://127.0.0.1:57117/callback",
    ],
)
def test_codex_cimd_rejects_nonmatching_loopback_redirect(monkeypatch, redirect_uri):
    monkeypatch.setattr(
        mcp,
        "_fetch_chatgpt_cimd",
        lambda requested_client: {"redirect_uris": ["http://127.0.0.1/callback"]},
    )
    with pytest.raises(mcp.MCPError, match="Redirect URI rejected"):
        mcp._validate_chatgpt_client(
            "https://chatgpt.com/oauth/codex/client.json", redirect_uri
        )


def test_oauth_transaction_uses_single_use_ttl_store_without_database(monkeypatch):
    items = {}

    class Dynamo:
        def put_item(self, **kwargs):
            items[kwargs["Item"]["state"]["S"]] = kwargs["Item"]

        def delete_item(self, **kwargs):
            return {"Attributes": items.pop(kwargs["Key"]["state"]["S"], None)}

    monkeypatch.setattr(mcp.boto3, "client", lambda name: Dynamo())
    settings = Settings(mcp_oauth_transactions_table_name="oauth-transactions")
    mcp._store_oauth_transaction(
        settings,
        state="facade-state",
        original_state="chatgpt-state",
        redirect_uri=mcp.CHATGPT_REDIRECT_URI,
        client_id=mcp.CHATGPT_CIMD_URL,
        resource="https://api.example/mcp",
    )
    assert mcp._consume_oauth_transaction(settings, "facade-state") == {
        "original_state": "chatgpt-state",
        "redirect_uri": mcp.CHATGPT_REDIRECT_URI,
        "client_id": mcp.CHATGPT_CIMD_URL,
        "resource": "https://api.example/mcp",
    }
    assert mcp._consume_oauth_transaction(settings, "facade-state") is None


def test_cimd_authorization_preserves_pkce_scope_and_resource(monkeypatch):
    stored = {}

    monkeypatch.setattr(
        mcp, "_store_oauth_transaction", lambda settings, **kwargs: stored.update(kwargs)
    )
    monkeypatch.setattr(
        mcp,
        "_fetch_chatgpt_cimd",
        lambda client_id: {
            "client_id": client_id,
            "redirect_uris": [mcp.CHATGPT_REDIRECT_URI],
        },
    )
    request = event(method="GET", path="/oauth/authorize", authorized=False)
    request["queryStringParameters"] = {
        "response_type": "code",
        "client_id": mcp.CHATGPT_CIMD_URL,
        "redirect_uri": mcp.CHATGPT_REDIRECT_URI,
        "scope": mcp.READ_SCOPE,
        "state": "chatgpt-state",
        "code_challenge": PKCE_VALUE,
        "code_challenge_method": "S256",
        "resource": "https://api.example/mcp",
    }
    response = mcp.handler(request, None)
    assert response["statusCode"] == 302
    provider = urlparse(response["headers"]["location"])
    query = parse_qs(provider.query)
    assert provider.path == "/oauth2/authorize"
    assert query["client_id"] == ["user-client"]
    assert query["redirect_uri"] == ["https://api.example/oauth/callback"]
    assert query["resource"] == ["https://api.example/mcp"]
    assert query["code_challenge_method"] == ["S256"]
    assert stored["original_state"] == "chatgpt-state"


def test_callback_specific_cimd_authorization_is_accepted(monkeypatch):
    callback_id = "connection_abc-123"
    client_id = f"https://chatgpt.com/oauth/{callback_id}/client.json"
    redirect_uri = f"https://chatgpt.com/connector/oauth/{callback_id}"
    stored = {}
    monkeypatch.setattr(
        mcp, "_store_oauth_transaction", lambda settings, **kwargs: stored.update(kwargs)
    )
    monkeypatch.setattr(
        mcp,
        "_fetch_chatgpt_cimd",
        lambda requested_client: {
            "client_id": requested_client,
            "redirect_uris": [redirect_uri],
        },
    )
    request = event(method="GET", path="/oauth/authorize", authorized=False)
    request["queryStringParameters"] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": mcp.READ_SCOPE,
        "state": "chatgpt-state",
        "code_challenge": PKCE_VALUE,
        "code_challenge_method": "S256",
        "resource": "https://api.example/mcp",
    }
    response = mcp.handler(request, None)
    assert response["statusCode"] == 302
    assert stored["client_id"] == client_id
    assert stored["redirect_uri"] == redirect_uri


def test_oauth_callback_returns_stable_redirect_and_rfc9207_issuer(monkeypatch):
    consumed = []

    def consume(settings, state):
        consumed.append(state)
        return {
            "redirect_uri": mcp.CHATGPT_REDIRECT_URI,
            "original_state": "chatgpt-state",
        }

    monkeypatch.setattr(mcp, "_consume_oauth_transaction", consume)
    request = event(method="GET", path="/oauth/callback", authorized=False)
    request["queryStringParameters"] = {"state": "facade-state", "code": "code-value"}
    response = mcp.handler(request, None)
    query = parse_qs(urlparse(response["headers"]["location"]).query)
    assert response["statusCode"] == 302
    assert query == {
        "code": ["code-value"],
        "iss": ["https://api.example"],
        "state": ["chatgpt-state"],
    }
    assert consumed == ["facade-state"]


def test_oauth_authorization_rejects_wrong_resource_without_provider_call(monkeypatch):
    monkeypatch.setattr(
        mcp,
        "_fetch_chatgpt_cimd",
        lambda client_id: {"redirect_uris": [mcp.CHATGPT_REDIRECT_URI]},
    )
    request = event(method="GET", path="/oauth/authorize", authorized=False)
    request["queryStringParameters"] = {
        "response_type": "code",
        "client_id": mcp.CHATGPT_CIMD_URL,
        "redirect_uri": mcp.CHATGPT_REDIRECT_URI,
        "scope": mcp.READ_SCOPE,
        "state": "state",
        "code_challenge": PKCE_VALUE,
        "code_challenge_method": "S256",
        "resource": "https://attacker.example/mcp",
    }
    response = mcp.handler(request, None)
    query = parse_qs(urlparse(response["headers"]["location"]).query)
    assert query["error"] == ["invalid_target"]
    assert query["iss"] == ["https://api.example"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("client_id", "https://attacker.example/oauth/client.json"),
        ("redirect_uri", "https://attacker.example/callback"),
        ("code_challenge_method", "plain"),
        ("code_challenge", "too-short"),
        ("state", ""),
    ],
)
def test_oauth_authorization_rejects_invalid_web_request(field, value, monkeypatch):
    request = event(method="GET", path="/oauth/authorize", authorized=False)
    request["queryStringParameters"] = {
        "response_type": "code",
        "client_id": mcp.CHATGPT_CIMD_URL,
        "redirect_uri": mcp.CHATGPT_REDIRECT_URI,
        "scope": mcp.READ_SCOPE,
        "state": "state",
        "code_challenge": PKCE_VALUE,
        "code_challenge_method": "S256",
        "resource": "https://api.example/mcp",
        field: value,
    }
    response = mcp.handler(request, None)
    assert response["statusCode"] == 400 or (
        response["statusCode"] == 302
        and parse_qs(urlparse(response["headers"]["location"]).query)["error"]
        == ["invalid_request"]
    )


def test_oauth_callback_rejects_unknown_or_replayed_state(monkeypatch):
    monkeypatch.setattr(mcp, "_consume_oauth_transaction", lambda settings, state: None)
    request = event(method="GET", path="/oauth/callback", authorized=False)
    request["queryStringParameters"] = {"state": "unknown", "code": "code"}
    response = mcp.handler(request, None)
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["message"] == "OAuth state expired"


def test_oauth_token_proxy_preserves_resource_and_validates_admin_token(monkeypatch):
    captured = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return json.dumps({"access_token": "bound-token", "token_type": "Bearer"}).encode()

    def fake_urlopen(request, timeout):
        captured.update(parse_qs(request.data.decode()))
        return Response()

    monkeypatch.setattr(
        mcp,
        "_fetch_chatgpt_cimd",
        lambda client_id: {"redirect_uris": [mcp.CHATGPT_REDIRECT_URI]},
    )
    monkeypatch.setattr(mcp.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        mcp,
        "_decode_access_token",
        lambda token, settings: {
            "client_id": "user-client",
            "scope": mcp.READ_SCOPE,
            "token_use": "access",
            "aud": "https://api.example/mcp",
            "cognito:groups": ["NurserySignalAdmins"],
        },
    )
    request = event(method="POST", path="/oauth/token", authorized=False)
    request["headers"] = {"content-type": "application/x-www-form-urlencoded"}
    request["body"] = urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": mcp.CHATGPT_CIMD_URL,
            "redirect_uri": mcp.CHATGPT_REDIRECT_URI,
            "code": "authorization-code",
            "code_verifier": PKCE_VALUE,
            "resource": "https://api.example/mcp",
        }
    )
    response = mcp.handler(request, None)
    assert response["statusCode"] == 200
    assert captured["client_id"] == ["user-client"]
    assert captured["redirect_uri"] == ["https://api.example/oauth/callback"]
    assert captured["resource"] == ["https://api.example/mcp"]


def test_chatgpt_web_token_exchange_accepts_verified_private_key_jwt(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        @staticmethod
        def read(limit):
            return json.dumps({"access_token": "bound-token", "token_type": "Bearer"}).encode()

    monkeypatch.setattr(
        mcp,
        "_validate_private_key_jwt",
        lambda assertion, **kwargs: captured.update(
            assertion=assertion, client_id=kwargs["client_id"]
        ),
    )
    monkeypatch.setattr(mcp.urllib.request, "urlopen", lambda request, timeout: Response())
    monkeypatch.setattr(
        mcp,
        "_decode_access_token",
        lambda token, settings: {
            "client_id": "user-client",
            "scope": mcp.READ_SCOPE,
            "token_use": "access",
            "aud": "https://api.example/mcp",
            "cognito:groups": ["NurserySignalAdmins"],
        },
    )
    assertion = mcp.jwt.encode(
        {
            "iss": mcp.CHATGPT_CIMD_URL,
            "sub": mcp.CHATGPT_CIMD_URL,
            "aud": "https://api.example/oauth/token",
            "iat": datetime.now(UTC),
            "exp": datetime.now(UTC) + timedelta(minutes=5),
            "jti": "assertion-id",
        },
        "not-used-by-mocked-validator-key-1234567890",
        algorithm="HS256",
    )
    request = event(method="POST", path="/oauth/token", authorized=False)
    request["body"] = urlencode(
        {
            "grant_type": "authorization_code",
            "redirect_uri": mcp.CHATGPT_REDIRECT_URI,
            "code": "authorization-code",
            "code_verifier": PKCE_VALUE,
            "resource": "https://api.example/mcp",
            "client_assertion_type": mcp.CLIENT_ASSERTION_TYPE,
            "client_assertion": assertion,
        }
    )
    response = mcp.handler(request, None)
    assert response["statusCode"] == 200
    assert captured == {"assertion": assertion, "client_id": mcp.CHATGPT_CIMD_URL}


def test_private_key_jwt_signature_issuer_subject_and_audience_are_verified(monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class SigningKey:
        key = private_key.public_key()

    class Jwks:
        def __init__(self, uri, cache_keys, timeout):
            assert uri == "https://chatgpt.com/oauth/jwks.json"
            assert cache_keys is True
            assert timeout == 5

        @staticmethod
        def get_signing_key_from_jwt(assertion):
            return SigningKey()

    monkeypatch.setattr(mcp, "PyJWKClient", Jwks)
    mcp._jwk_clients.clear()
    settings = Settings(mcp_oauth_issuer="https://api.example")
    now = datetime.now(UTC)
    claims = {
        "iss": mcp.CHATGPT_CIMD_URL,
        "sub": mcp.CHATGPT_CIMD_URL,
        "aud": "https://api.example/oauth/token",
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "jti": "assertion-id",
    }
    assertion = mcp.jwt.encode(
        claims, private_key, algorithm="RS256", headers={"kid": "test"}
    )
    mcp._validate_private_key_jwt(
        assertion,
        client_id=mcp.CHATGPT_CIMD_URL,
        metadata=mcp.CHATGPT_CIMD,
        settings=settings,
    )

    claims["aud"] = "https://attacker.example/oauth/token"
    wrong_audience = mcp.jwt.encode(
        claims, private_key, algorithm="RS256", headers={"kid": "test"}
    )
    with pytest.raises(mcp.MCPError, match="assertion is invalid"):
        mcp._validate_private_key_jwt(
            wrong_audience,
            client_id=mcp.CHATGPT_CIMD_URL,
            metadata=mcp.CHATGPT_CIMD,
            settings=settings,
        )


def test_chatgpt_web_token_exchange_rejects_missing_or_mismatched_assertion(monkeypatch):
    request = event(method="POST", path="/oauth/token", authorized=False)
    base = {
        "grant_type": "authorization_code",
        "client_id": mcp.CHATGPT_CIMD_URL,
        "redirect_uri": mcp.CHATGPT_REDIRECT_URI,
        "code": "authorization-code",
        "code_verifier": PKCE_VALUE,
        "resource": "https://api.example/mcp",
    }
    request["body"] = urlencode(base)
    assert mcp.handler(request, None)["statusCode"] == 401

    assertion = mcp.jwt.encode(
        {"iss": mcp.CODEX_CIMD_URL},
        "not-verified-test-key-12345678901234567890",
        algorithm="HS256",
    )
    request["body"] = urlencode(
        {
            **base,
            "client_assertion_type": mcp.CLIENT_ASSERTION_TYPE,
            "client_assertion": assertion,
        }
    )
    assert mcp.handler(request, None)["statusCode"] == 401


def test_access_token_validation_accepts_bound_user_and_rejects_wrong_audience(monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk["kid"] = "test"

    settings = Settings(
        mcp_token_issuer="https://tokens.example",
        mcp_token_jwks=json.dumps({"keys": [public_jwk]}),
        mcp_resource_url="https://api.example/mcp",
        mcp_user_client_id="user-client",
        mcp_service_client_id="service-client",
    )
    monkeypatch.setattr(
        mcp.urllib.request,
        "urlopen",
        lambda *args, **kwargs: pytest.fail("JWT validation must not require runtime egress"),
    )
    now = datetime.now(UTC)
    claims = {
        "iss": "https://tokens.example",
        "sub": "admin-user",
        "client_id": "user-client",
        "token_use": "access",
        "scope": mcp.READ_SCOPE,
        "aud": "https://api.example/mcp",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    token = mcp.jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test"})
    assert REAL_DECODE_ACCESS_TOKEN(token, settings)["aud"] == "https://api.example/mcp"
    claims["aud"] = "https://other.example/mcp"
    wrong = mcp.jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test"})
    with pytest.raises(mcp.MCPError, match="audience"):
        REAL_DECODE_ACCESS_TOKEN(wrong, settings)


def test_tool_call_is_structured_audited_and_does_not_call_provider(monkeypatch):
    calls = []
    monkeypatch.setitem(
        mcp.HANDLERS,
        "get_operations_summary",
        lambda settings, args: {"schema_version": "operations-v1", "read_only": True},
    )
    monkeypatch.setattr(mcp, "_start_audit", lambda *args: calls.append("start") or "audit")
    monkeypatch.setattr(mcp, "_finish_audit", lambda *args, **kwargs: calls.append(kwargs))
    response = mcp.handler(
        event(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "get_operations_summary", "arguments": {}},
            }
        ),
        None,
    )
    result = json.loads(response["body"])["result"]
    assert result["structuredContent"]["read_only"] is True
    assert result["isError"] is False
    assert calls[0] == "start"
    assert calls[1]["success"] is True


def test_invalid_tool_and_argument_errors_do_not_leak(monkeypatch):
    unknown = mcp.handler(
        event(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "publish_opportunity", "arguments": {}},
            }
        ),
        None,
    )
    assert json.loads(unknown["body"])["error"]["data"]["error"] == "invalid_argument"
    with pytest.raises(mcp.MCPError, match="limit"):
        mcp._paging({"limit": 101})
    with pytest.raises(mcp.MCPError, match="cursor"):
        mcp._paging({"cursor": "not-a-cursor"})


def test_signal_projection_redacts_raw_evidence_and_private_storage(monkeypatch):
    signal = {
        "id": "8fce607b-b153-4ec5-a958-b0b9e5fcbe15",
        "vertical": "CHILDRENS_HOME",
        "source_type": "planning",
        "source_url": "https://planning.example/application/1",
        "external_id": "24/1",
        "title": "Change of use to children's home",
        "raw_text": "private raw payload",
        "documents": [{"s3_bucket": "private", "s3_key": "secret/key"}],
        "planning_revisions": [],
        "ai_reviews": [],
        "enrichment": {"review_status": "APPROVED", "extracted_facts": {}},
    }
    monkeypatch.setattr(mcp, "signal_detail", lambda settings, signal_id: signal)

    class Result:
        def fetchall(self):
            return []

    class Conn:
        def execute(self, *args):
            return Result()

    @contextmanager
    def fake_connection(settings):
        yield Conn()

    monkeypatch.setattr(mcp, "connection", fake_connection)
    result = mcp._tool_get_signal(Settings(), {"signal_id": signal["id"]})
    encoded = json.dumps(result)
    assert "private raw payload" not in encoded
    assert "secret/key" not in encoded
    assert result["signal"]["evidence"]["document_count"] == 1


def test_not_found_and_machine_readable_tool_error(monkeypatch):
    monkeypatch.setattr(mcp, "signal_detail", lambda settings, signal_id: None)
    with pytest.raises(mcp.MCPError) as caught:
        mcp._tool_get_signal(Settings(), {"signal_id": "8fce607b-b153-4ec5-a958-b0b9e5fcbe15"})
    assert caught.value.code == "not_found"


def test_search_signals_is_bounded_and_cursor_based(monkeypatch):
    monkeypatch.setattr(
        mcp,
        "list_signals",
        lambda settings, **kwargs: {"items": [], "total": 45, **kwargs},
    )
    result = mcp._tool_search_signals(Settings(), {"limit": 20})
    assert result["count"] == 0
    assert result["total"] == 45
    assert result["next_cursor"]
    assert mcp._offset(result["next_cursor"]) == 20


def test_recent_changes_reads_policy_version_from_parent_runs(monkeypatch):
    statements = []

    class Result:
        def fetchall(self):
            return []

    class Conn:
        def execute(self, statement, params=None):
            statements.append(statement)
            return Result()

    @contextmanager
    def fake_connection(settings):
        yield Conn()

    monkeypatch.setattr(mcp, "connection", fake_connection)
    result = mcp._tool_recent(Settings(), {"last_hours": 24, "limit": 20})

    assert result["items"] == []
    query = statements[0]
    assert "JOIN care_publication_runs r ON r.id = i.run_id" in query
    assert "JOIN care_withdrawal_runs r ON r.id = i.run_id" in query
    assert "'policy_version', r.policy_version" in query


def test_rate_limit_rejects_client_before_audit_insert(monkeypatch):
    class Result:
        def fetchone(self):
            return (60,)

    class Conn:
        def execute(self, statement, params=None):
            assert "INSERT INTO mcp_request_audit" not in statement
            return Result()

    @contextmanager
    def fake_connection(settings):
        yield Conn()

    monkeypatch.setattr(mcp, "connection", fake_connection)
    with pytest.raises(mcp.MCPError) as caught:
        mcp._start_audit(
            Settings(mcp_rate_limit_per_minute=60),
            {"client_id": "client"},
            "get_operations_summary",
            {},
        )
    assert caught.value.code == "unavailable"


def test_mcp_infrastructure_is_dedicated_and_has_no_provider_permissions():
    terraform = open("infra/mcp.tf", encoding="utf-8").read()
    assert 'handler          = "app.mcp.handler"' in terraform
    assert "signalhub-mcp/read" in terraform
    assert 'allowed_oauth_flows                  = ["code"]' in terraform
    assert 'route_key = "GET /oauth/authorize"' in terraform
    assert 'route_key = "POST /oauth/token"' in terraform
    assert "aws_apigatewayv2_authorizer.mcp" not in terraform
    assert "MCP_OAUTH_CALLBACK_URL" in terraform
    assert "PLANNING_PROVIDER" not in terraform
    assert "sqs:SendMessage" not in terraform
    assert "bedrock:InvokeModel" not in terraform


def test_mcp_module_has_no_business_mutation_sql_or_provider_client():
    source = open("backend/app/mcp.py", encoding="utf-8").read()
    insert_targets = [line for line in source.splitlines() if "INSERT INTO" in line]
    update_targets = [line for line in source.splitlines() if "UPDATE " in line]
    delete_targets = [line for line in source.splitlines() if "DELETE FROM" in line]
    assert insert_targets == [
        '            """INSERT INTO mcp_request_audit',
        '            """INSERT INTO mcp_oauth_transactions',
    ]
    assert update_targets == ['                """UPDATE mcp_request_audit']
    assert all("mcp_oauth_transactions" in line for line in delete_targets)
    assert "planning_provider" not in source
    assert "requests." not in source

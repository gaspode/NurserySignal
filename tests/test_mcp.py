import json
from contextlib import contextmanager

import pytest
from app import mcp
from app.config import Settings


def event(body=None, *, authorized=True, method="POST", path="/mcp"):
    value = {
        "rawPath": path,
        "headers": {"authorization": "Bearer redacted"} if authorized else {},
        "requestContext": {"http": {"method": method}},
    }
    if authorized:
        value["requestContext"]["authorizer"] = {
            "jwt": {
                "claims": {
                    "client_id": "service-client",
                    "sub": "test-service",
                    "scope": mcp.READ_SCOPE,
                }
            }
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


def test_authentication_and_read_scope_are_required(monkeypatch):
    assert mcp.handler(event({}, authorized=False), None)["statusCode"] == 401
    missing_scope = event({})
    missing_scope["requestContext"]["authorizer"]["jwt"]["claims"]["scope"] = "openid"
    assert mcp.handler(missing_scope, None)["statusCode"] == 401
    wrong_client = event({})
    wrong_client["requestContext"]["authorizer"]["jwt"]["claims"]["client_id"] = "wrong"
    assert mcp.handler(wrong_client, None)["statusCode"] == 401


def test_admin_user_requires_admin_group(monkeypatch):
    request = event({})
    claims = request["requestContext"]["authorizer"]["jwt"]["claims"]
    claims.update({"client_id": "user-client", "cognito:groups": "NurserySignalAdmins"})
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
    assert body["scopes_supported"] == ["signalhub-mcp/read"]


def test_oauth_server_metadata_advertises_cognito_pkce_facade():
    response = mcp.handler(
        event(authorized=False, method="GET", path="/.well-known/oauth-authorization-server"),
        None,
    )
    body = json.loads(response["body"])
    assert body["issuer"] == "https://api.example"
    assert body["authorization_endpoint"] == "https://login.example/oauth2/authorize"
    assert body["token_endpoint"] == "https://login.example/oauth2/token"
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert body["authorization_response_iss_parameter_supported"] is False


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
    assert "authorization_scopes = [local.mcp_read_scope]" in terraform
    assert "PLANNING_PROVIDER" not in terraform
    assert "sqs:SendMessage" not in terraform
    assert "bedrock:InvokeModel" not in terraform


def test_mcp_module_has_no_business_mutation_sql_or_provider_client():
    source = open("backend/app/mcp.py", encoding="utf-8").read()
    insert_targets = [line for line in source.splitlines() if "INSERT INTO" in line]
    update_targets = [line for line in source.splitlines() if "UPDATE " in line]
    assert insert_targets == ['            """INSERT INTO mcp_request_audit']
    assert update_targets == ['                """UPDATE mcp_request_audit']
    assert "planning_provider" not in source
    assert "requests." not in source

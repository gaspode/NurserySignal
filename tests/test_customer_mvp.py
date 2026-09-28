from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from app.config import Settings
from app.customer import (
    _eligibility_sql,
    _project_opportunity,
    apply_pilot_publications,
    list_customer_opportunities,
    pilot_curation_inventory,
)
from app.customer_digest import queue_weekly_digests, sender_handler
from app.handler import handler

ADMIN = {"sub": "admin", "cognito:groups": ["NurserySignalAdmins"]}
CUSTOMER = {"sub": "customer", "cognito:groups": ["CareSignalCustomers"]}


def event(path: str, method: str = "GET", *, claims: dict | None = None, query=None, body=None):
    return {
        "rawPath": path,
        "body": body,
        "queryStringParameters": query or {},
        "requestContext": {
            "http": {"method": method},
            "authorizer": {"jwt": {"claims": claims or CUSTOMER}},
        },
    }


def customer_context(*, nationwide: bool = True) -> dict:
    return {
        "user_id": "user-1",
        "account_id": "account-1",
        "email": "pilot@example.test",
        "display_name": "Pilot",
        "role": "OWNER",
        "user_status": "ACTIVE",
        "account_name": "Pilot supplier",
        "account_status": "PILOT",
        "plan": "PRO" if nationwide else "STARTER",
        "allowed_regions": [] if nationwide else ["West Midlands"],
        "allowed_local_authorities": [],
        "entitlements": {
            "nationwide": nationwide,
            "saved_searches": nationwide,
            "alert_frequencies": ["OFF", "WEEKLY"],
        },
    }


def test_customer_role_cannot_access_admin_or_ingestion(monkeypatch) -> None:
    monkeypatch.setattr("app.handler.customer_context", lambda *_: customer_context())
    assert handler(event("/admin/signals"), None)["statusCode"] == 403
    assert handler(event("/signals", "POST", body="{}"), None)["statusCode"] == 403


def test_customer_feed_requires_customer_membership(monkeypatch) -> None:
    called = False

    def should_not_run(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("app.handler.list_customer_opportunities", should_not_run)
    response = handler(event("/customer/opportunities", claims=ADMIN), None)
    assert response["statusCode"] == 403
    assert called is False


def test_customer_feed_forwards_only_bounded_safe_filters(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr("app.handler.customer_context", lambda *_: customer_context())

    def fake_list(_settings, _context, **kwargs):
        captured.update(kwargs)
        return {"items": [], "total": 0}

    monkeypatch.setattr("app.handler.list_customer_opportunities", fake_list)
    response = handler(
        event(
            "/customer/opportunities",
            query={"limit": "999", "region": "North West", "source_type": "planning"},
        ),
        None,
    )
    assert response["statusCode"] == 200
    assert captured["limit"] == 50
    assert captured["region"] == "North West"
    assert captured["source_type"] == "planning"


def test_customer_projection_redacts_internal_exact_location() -> None:
    item = _project_opportunity(
        {
            "id": uuid4(),
            "operator_name": "Example Care Ltd",
            "town": "Coventry",
            "local_authority": "Coventry",
            "region": "West Midlands",
            "postcode": "CV1 2AB",
            "location_sensitivity": "INTERNAL_EXACT",
            "change_type": "OPENING",
            "lifecycle_stage": "PLANNING",
            "source_types": ["planning"],
            "first_seen_at": datetime(2026, 1, 1, tzinfo=UTC),
            "latest_update_at": datetime(2026, 2, 1, tzinfo=UTC),
        },
        saved=False,
    )
    assert item["postcode"] == "CV1"
    assert item["title"] == "Example Care Ltd — new children’s home, Coventry CV1"
    assert item["location_precision"] == "AREA_ONLY"
    assert "address" not in item
    assert "confidence" not in item
    assert "review_status" not in item


def test_customer_eligibility_excludes_rejected_and_procurement_only() -> None:
    sql = _eligibility_sql()
    assert "publication_status = 'PUBLISHED'" in sql
    assert "review_status NOT IN ('REJECTED', 'MERGED')" in sql
    assert "eligible_se.review_status = 'APPROVED'" in sql
    assert "eligible_rs.source_type <> 'procurement'" in sql


def test_starter_geography_is_enforced_in_database_query(monkeypatch) -> None:
    executed = []

    class Cursor:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return self.value

        def fetchall(self):
            return self.value

    class Connection:
        def execute(self, sql, params=None):
            executed.append((sql, params))
            return Cursor((0,) if sql.startswith("SELECT count") else [])

    @contextmanager
    def fake_connection(_settings):
        yield Connection()

    monkeypatch.setattr("app.customer.connection", fake_connection)
    result = list_customer_opportunities(
        SimpleNamespace(), customer_context(nationwide=False), limit=25, offset=0
    )
    assert result["total"] == 0
    assert all("geo.region" in sql for sql, _ in executed)
    assert all(["west midlands"] in params for _, params in executed)


def test_admin_can_provision_but_customer_cannot(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.handler.provision_customer_account",
        lambda *_: {"id": "account-1", "owner_email": "pilot@example.test"},
    )
    payload = json.dumps(
        {
            "name": "Pilot supplier",
            "email": "pilot@example.test",
            "plan": "STARTER",
            "allowed_regions": ["North West"],
        }
    )
    denied = handler(event("/admin/customer-accounts", "POST", body=payload), None)
    allowed = handler(event("/admin/customer-accounts", "POST", body=payload, claims=ADMIN), None)
    assert denied["statusCode"] == 403
    assert allowed["statusCode"] == 201


def test_weekly_digest_remains_disabled_without_verified_sender() -> None:
    assert queue_weekly_digests(Settings(), now=datetime(2026, 9, 28, tzinfo=UTC)) == {
        "eligible": 0,
        "queued": 0,
        "duplicates": 0,
        "errors": 0,
    }


def test_pilot_publication_is_explicit_bounded_and_audited(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        "app.customer.set_opportunity_publication",
        lambda settings, opportunity_id, payload, actor: calls.append(
            (opportunity_id, payload, actor)
        )
        or {"id": opportunity_id},
    )
    result = apply_pilot_publications(
        SimpleNamespace(),
        [{"id": str(uuid4()), "customer_title": "New children’s home — Coventry"}],
        actor="pilot-operator",
    )
    assert result["published"] == 1
    assert calls[0][1]["status"] == "PUBLISHED"
    assert calls[0][2] == "pilot-operator"


def test_pilot_publication_rejects_duplicates_and_unbounded_batches(monkeypatch) -> None:
    identifier = str(uuid4())
    with __import__("pytest").raises(ValueError, match="duplicate"):
        apply_pilot_publications(
            SimpleNamespace(), [{"id": identifier}, {"id": identifier}], actor="operator"
        )
    with __import__("pytest").raises(ValueError, match="1 to 30"):
        apply_pilot_publications(
            SimpleNamespace(), [{"id": str(uuid4())} for _ in range(31)], actor="operator"
        )


def test_pilot_inventory_returns_customer_preview_without_raw_evidence(monkeypatch) -> None:
    opportunity_id = uuid4()
    signal_id = uuid4()
    now = datetime(2026, 9, 28, tzinfo=UTC)

    class Cursor:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    class Connection:
        calls = 0

        def execute(self, _sql, _params=None):
            self.calls += 1
            if self.calls == 1:
                return Cursor(
                    [
                        (
                            opportunity_id,
                            "Planning description that remains internal",
                            None,
                            None,
                            "Example Care Ltd",
                            "Coventry",
                            "CV1 2AB",
                            "OPENING",
                            "PLANNING",
                            0.86,
                            "APPROVED",
                            "DRAFT",
                            now,
                            now,
                            "INTERNAL_EXACT",
                            "Planning evidence indicates a new children’s home",
                            "Planning",
                            now,
                        )
                    ]
                )
            return Cursor(
                [
                    (
                        opportunity_id,
                        signal_id,
                        "planning",
                        now,
                        "Change of use to children’s home",
                        "https://example.gov.uk/planning/1",
                        "REF-1",
                        {"region": "West Midlands", "local_authority": "Coventry"},
                        "APPROVED",
                        0.88,
                        {"event_type": "opening"},
                        "SYSTEM",
                        "created from strong planning evidence",
                    )
                ]
            )

    @contextmanager
    def fake_connection(_settings):
        yield Connection()

    monkeypatch.setattr("app.customer.connection", fake_connection)
    result = pilot_curation_inventory(SimpleNamespace(), limit=200)
    assert result["limit"] == 100
    assert result["items"][0]["eligible_for_publication"] is True
    assert result["items"][0]["customer_preview"]["postcode"] == "CV1"
    assert "raw_text" not in json.dumps(result)


def test_internal_pilot_operations_are_not_exposed_through_http(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.handler.pilot_curation_inventory", lambda *_args, **_kwargs: {"count": 0}
    )
    direct = handler({"operation": "customer_pilot_inventory", "limit": 5}, None)
    assert direct == {"count": 0}
    monkeypatch.setattr("app.handler.customer_context", lambda *_: customer_context())
    response = handler(event("/customer/pilot-inventory", claims=CUSTOMER), None)
    assert response["statusCode"] == 404


def test_digest_sender_uses_ses_and_reports_delivery(monkeypatch) -> None:
    sent = []
    invoked = []

    class Ses:
        def send_email(self, **kwargs):
            sent.append(kwargs)

    class Lambda:
        def invoke(self, **kwargs):
            invoked.append(kwargs)

    monkeypatch.setenv("CARESIGNAL_EMAIL_FROM", "updates@example.test")
    monkeypatch.setenv("CARESIGNAL_EMAIL_FROM_NAME", "CareSignal")
    monkeypatch.setenv("BACKEND_FUNCTION_NAME", "nurserysignal-prod-backend")
    monkeypatch.setattr(
        "app.customer_digest.boto3.client",
        lambda service: Ses() if service == "sesv2" else Lambda(),
    )
    result = sender_handler(
        {
            "Records": [
                {
                    "messageId": "message-1",
                    "body": json.dumps(
                        {
                            "run_id": str(uuid4()),
                            "to": "pilot@example.test",
                            "subject": "CareSignal weekly update",
                            "html": "<h1>CareSignal</h1>",
                        }
                    ),
                }
            ]
        },
        None,
    )
    assert result == {"batchItemFailures": []}
    assert sent[0]["FromEmailAddress"] == "CareSignal <updates@example.test>"
    assert sent[0]["Destination"] == {"ToAddresses": ["pilot@example.test"]}
    notification = json.loads(invoked[0]["Payload"])
    assert notification["operation"] == "customer_digest_delivery"
    assert notification["status"] == "SENT"

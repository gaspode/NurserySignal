from __future__ import annotations

import io
import json
from contextlib import contextmanager
from types import SimpleNamespace

from app.handler import handler
from app.planning_contact_preview import (
    CONTACT_FIELD_DISPLAY_POLICY,
    nursery_customer_contact_preview,
)


def test_nursery_contact_preview_is_bounded_role_labelled_and_not_customer_exposed(
    monkeypatch,
) -> None:
    class Cursor:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    class Connection:
        def execute(self, _sql, params=None):
            assert params == (50,)
            return Cursor(
                [
                    (
                        "op-1",
                        "New nursery — Example",
                        "Planning evidence supports a nursery opening.",
                        "10 Example Road, Leeds",
                        "LS1 1AA",
                        "Leeds",
                        "OPENING",
                        "PLANNING_PENDING",
                        "signal-1",
                        "plota-1",
                        {"planning_reference": "REF-1", "council": "Leeds"},
                        "Change of use to day nursery",
                    )
                ]
            )

    @contextmanager
    def fake_connection(_settings):
        yield Connection()

    class Lambda:
        def invoke(self, **kwargs):
            request = json.loads(kwargs["Payload"])
            assert request["operation"] == "planning_contact_data_preview"
            assert request["applications"] == [
                {"signal_id": "signal-1", "provider_application_id": "plota-1"}
            ]
            return {
                "Payload": io.BytesIO(
                    json.dumps(
                        {
                            "provider_requests": 1,
                            "results": [
                                {
                                    "signal_id": "signal-1",
                                    "applicant": "Jane Example",
                                    "agent": "Planning Agent Ltd",
                                    "agent_company": None,
                                }
                            ],
                        }
                    ).encode()
                )
            }

    monkeypatch.setattr("app.planning_contact_preview.connection", fake_connection)
    monkeypatch.setattr("app.planning_contact_preview.boto3.client", lambda _name: Lambda())
    result = nursery_customer_contact_preview(
        SimpleNamespace(planning_collector_function_name="collector"), actor="admin", limit=99
    )
    assert result["examined"] == 1
    assert result["provider_requests"] == 1
    assert result["customer_exposure"] == "DISABLED_PENDING_POLICY_LEGAL_REVIEW"
    item = result["samples"][0]
    assert item["contact_context"] == "PARTIAL_CONTACT_CONTEXT"
    assert item["contact_provenance"]["applicant_role"] == "APPLICANT"
    assert item["contact_provenance"]["agent_role"] == "AGENT"
    assert item["contact_provenance"]["operator_status"] == "NOT_CONFIRMED"
    assert item["customer_safe_preview"]["operator"] == "Not confirmed"
    rendered_sample = json.dumps(item)
    assert "email" not in rendered_sample.casefold()
    assert "phone" not in rendered_sample.casefold()
    assert CONTACT_FIELD_DISPLAY_POLICY["applicant_name_person"] == "REQUIRES_POLICY_LEGAL_REVIEW"


def test_nursery_contact_preview_marks_company_context_actionable_without_operator_promotion(
    monkeypatch,
) -> None:
    class Cursor:
        def fetchall(self):
            return [
                (
                    "op-1", "New nursery", None, "1 Example Road", "LS1 1AA", "Leeds",
                    "OPENING", "PLANNING_APPROVED", "signal-1", "plota-1", {}, "Nursery",
                )
            ]

    class Connection:
        def execute(self, _sql, _params=None):
            return Cursor()

    @contextmanager
    def fake_connection(_settings):
        yield Connection()

    class Lambda:
        def invoke(self, **_kwargs):
            return {
                "Payload": io.BytesIO(
                    b'{"provider_requests":1,"results":[{"signal_id":"signal-1",'
                    b'"applicant":"Example Nurseries Limited"}]}'
                )
            }

    monkeypatch.setattr("app.planning_contact_preview.connection", fake_connection)
    monkeypatch.setattr("app.planning_contact_preview.boto3.client", lambda _name: Lambda())
    result = nursery_customer_contact_preview(
        SimpleNamespace(planning_collector_function_name="collector"), actor="admin", limit=1
    )
    item = result["samples"][0]
    assert item["contact_context"] == "ACTIONABLE_CONTACT_CONTEXT"
    assert item["contact_provenance"]["applicant_type"] == "COMPANY_LIKE"
    assert item["contact_provenance"]["operator_status"] == "NOT_CONFIRMED"


def test_nursery_contact_preview_http_is_admin_only(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.handler.nursery_customer_contact_preview",
        lambda *_args, **_kwargs: {"preview_only": True},
    )
    base = {
        "rawPath": "/admin/nursery-customer-contact-preview",
        "body": '{"limit": 25}',
        "queryStringParameters": {},
        "requestContext": {
            "http": {"method": "POST"},
            "authorizer": {"jwt": {"claims": {"sub": "customer", "cognito:groups": []}}},
        },
    }
    denied = handler(base, None)
    assert denied["statusCode"] == 403
    base["requestContext"]["authorizer"]["jwt"]["claims"] = {
        "sub": "admin",
        "cognito:groups": ["NurserySignalAdmins"],
    }
    allowed = handler(base, None)
    assert allowed["statusCode"] == 200
    assert json.loads(allowed["body"])["preview_only"] is True

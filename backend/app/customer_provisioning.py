from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger("nurserysignal")


def _subject(user: dict[str, Any]) -> str:
    attributes = {item["Name"]: item["Value"] for item in user.get("UserAttributes", [])}
    value = str(attributes.get("sub") or "").strip()
    if not value:
        raise RuntimeError("Cognito did not return a user subject")
    return value


def _record_account(
    lambda_client: Any, backend_function: str, request: dict[str, Any], sub: str
) -> None:
    response = lambda_client.invoke(
        FunctionName=backend_function,
        InvocationType="RequestResponse",
        Payload=json.dumps(
            {"operation": "customer_pilot_record", "cognito_sub": sub, **request},
            separators=(",", ":"),
        ).encode(),
    )
    payload = json.loads(response["Payload"].read() or b"{}")
    if response.get("FunctionError") or payload.get("errorMessage"):
        raise RuntimeError("customer account persistence failed")


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    pool_id = os.environ.get("COGNITO_USER_POOL_ID", "").strip()
    group = os.environ.get("CUSTOMER_GROUP", "CareSignalCustomers").strip()
    backend_function = os.environ.get("BACKEND_FUNCTION_NAME", "").strip()
    if not pool_id or not group or not backend_function:
        raise RuntimeError("customer provisioning worker is not configured")
    cognito = boto3.client("cognito-idp")
    lambda_client = boto3.client("lambda")
    failures = []
    for record in event.get("Records", []):
        message_id = record.get("messageId")
        created = False
        email = ""
        try:
            request = json.loads(record.get("body") or "{}")
            email = str(request.get("email") or "").strip().lower()
            try:
                response = cognito.admin_create_user(
                    UserPoolId=pool_id,
                    Username=email,
                    UserAttributes=[
                        {"Name": "email", "Value": email},
                        {"Name": "email_verified", "Value": "true"},
                    ],
                    DesiredDeliveryMediums=["EMAIL"],
                )
                user = response["User"]
                created = True
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "UsernameExistsException":
                    raise
                user = cognito.admin_get_user(UserPoolId=pool_id, Username=email)
            sub = _subject(user)
            cognito.admin_add_user_to_group(UserPoolId=pool_id, Username=email, GroupName=group)
            _record_account(lambda_client, backend_function, request, sub)
        except Exception as exc:
            logger.error("customer_provisioning_failed error_type=%s", type(exc).__name__)
            if created and email:
                try:
                    cognito.admin_delete_user(UserPoolId=pool_id, Username=email)
                except Exception:
                    logger.error("customer_provisioning_rollback_failed")
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}

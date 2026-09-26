import json

import pytest
from app.collector_events import collector_payload


def test_direct_collector_payload_is_unchanged() -> None:
    payload = {"source": "scheduled", "max_records": 50}
    assert collector_payload(payload) is payload


def test_sqs_collector_command_is_unwrapped() -> None:
    payload = {"source": "manual", "max_records": 25, "run_id": "run-1"}
    event = {"Records": [{"eventSource": "aws:sqs", "body": json.dumps(payload)}]}
    assert collector_payload(event) == payload


@pytest.mark.parametrize(
    "event",
    [
        {"Records": []},
        {"Records": [{"eventSource": "aws:sqs", "body": "not-json"}]},
        {"Records": [{"eventSource": "aws:s3", "body": "{}"}]},
    ],
)
def test_invalid_collector_commands_are_rejected(event: dict) -> None:
    with pytest.raises(ValueError):
        collector_payload(event)

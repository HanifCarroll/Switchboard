"""The stack's small email formatter replaces raw metric text with useful guidance."""

import json
from pathlib import Path
from unittest.mock import Mock


def test_operational_email_has_a_clear_subject_summary_and_links(monkeypatch):

    # 1. Load the deployed handler source and supply a representative alarm.
    template = json.loads(
        (Path(__file__).parents[2] / "aws" / "template.json").read_text()
    )
    source = template["Resources"]["Notifications"]["Properties"]["Code"]["ZipFile"]
    namespace = {}
    exec(compile(source, "notification_inline", "exec"), namespace)
    client = Mock()
    monkeypatch.setenv("ALERTS_TOPIC_ARN", "arn:aws:sns:us-east-1:123456789012:alerts")
    monkeypatch.setattr("boto3.client", lambda name: client)
    alarm = {
        "AlarmName": "switchboard-WorkflowFailures",
        "AlarmDescription": template["Resources"]["WorkflowFailuresAlarm"][
            "Properties"
        ]["AlarmDescription"],
        "StateChangeTime": "2026-10-01T11:50:58+00:00",
        "NewStateReason": "Threshold Crossed: 1 datapoint was greater than 0.0",
    }

    # 2. Verify readable delivery and a clearly labeled preview.
    result = namespace["handler"](
        {"Records": [{"Sns": {"Message": json.dumps(alarm)}}]}, None
    )
    assert result == {"formatted": 1}
    sent = client.publish.call_args.kwargs
    assert sent["Subject"] == "Switchboard: A change workflow failed"
    assert "At least one change workflow failed" in sent["Message"]
    assert "do not repeat an action blindly" in sent["Message"]
    assert "01 Oct 2026, 11:50 UTC" in sent["Message"]
    assert "#alarmsV2:alarm/switchboard-WorkflowFailures" in sent["Message"]
    assert "Threshold Crossed" not in sent["Message"]
    alarm["TestNotification"] = True
    namespace["handler"]({"Records": [{"Sns": {"Message": json.dumps(alarm)}}]}, None)
    assert client.publish.call_args.kwargs["Subject"].startswith("TEST — ")
    assert "no new failure was detected" in client.publish.call_args.kwargs["Message"]

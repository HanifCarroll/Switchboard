"""SAM releases preserve runtime settings and recover from unhealthy deployments."""

import importlib.util
import io
import json
from collections import OrderedDict
from pathlib import Path
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location(
    "aws_release", Path(__file__).resolve().parents[2] / "scripts" / "aws.py"
)
assert spec is not None and spec.loader is not None
aws = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aws)


@pytest.mark.parametrize("employee_id", ["emp-alex", "wrong-identity"])
def test_live_smoke_check_rejects_a_healthy_website_with_a_broken_api(
    monkeypatch, employee_id
):
    monkeypatch.setattr(aws, "outputs", lambda session: {"DeliveryURL": "https://demo"})
    monkeypatch.setattr(aws.time, "sleep", lambda seconds: None)

    class Response(io.BytesIO):
        status = 200

    def response(url, **arguments):
        body = {
            "https://demo/": '<title>Switchboard</title><script src="/_next/static/client.js"></script>',
            "https://demo/_next/static/client.js": "console.log('loaded')",
            "https://demo/api/me": json.dumps({"employee_id": employee_id}),
            "https://demo/api/demo/personas": '[{"id":"emp-priya"}]',
        }[url]
        return Response(body.encode())

    monkeypatch.setattr(aws, "build_opener", lambda handler: Mock(open=response))
    if employee_id == "emp-alex":
        aws.smoke_check(Mock())
    else:
        with pytest.raises(RuntimeError, match="cannot open a demo workspace"):
            aws.smoke_check(Mock())


@pytest.mark.parametrize("health_fails", [False, True])
def test_sam_release_preserves_runtime_and_restores_previous_template(
    tmp_path, monkeypatch, health_fails
):
    monkeypatch.setattr(aws, "LOCAL", tmp_path)
    monkeypatch.setattr(aws, "check_stack", Mock())
    session = Mock()
    client = session.client.return_value
    parameters = {
        "ModelProvider": "bedrock",
        "BedrockModel": "minimax.minimax-m2.5",
        "AlertEmail": "operator@example.com",
        "DailyJobLimit": "100",
    }
    client.describe_stacks.return_value = {
        "Stacks": [
            {
                "Parameters": [
                    {"ParameterKey": key, "ParameterValue": value}
                    for key, value in parameters.items()
                ]
            }
        ]
    }
    template = {
        "Resources": {
            "Worker": {"Properties": {"CodeUri": "s3://artifacts/previous-worker"}}
        },
        "Transform": ["AWS::LanguageExtensions", "AWS::Serverless-2016-10-31"],
    }
    client.get_template.return_value = {"TemplateBody": OrderedDict(template)}
    deploy = Mock()
    monkeypatch.setattr(aws, "deploy_template", deploy)
    health = Mock(
        side_effect=[RuntimeError("API unavailable"), None] if health_fails else None
    )
    monkeypatch.setattr(aws, "smoke_check", health)
    if health_fails:
        with pytest.raises(RuntimeError, match="SAM deployment failed"):
            aws.release(session, model_provider="deepseek")
    else:
        aws.release(session)
    assert (
        json.loads((tmp_path / "previous-deployment.json").read_text())["parameters"]
        == parameters
    )
    assert json.loads((tmp_path / "previous-template.yml").read_text()) == template
    receipt = json.loads((tmp_path / "deployment-health.json").read_text())
    if health_fails:
        assert deploy.call_count == 2
        assert deploy.call_args.args == (
            session,
            tmp_path / "previous-template.yml",
            parameters,
        )
        assert receipt["rolled_back"] is True
        assert receipt["status"] == "failed"
    else:
        assert deploy.call_count == 1
        assert deploy.call_args.args == (session, aws.BUILD / "template.yaml", {})
        assert receipt["status"] == "passed"
    client.update_stack.assert_not_called()


def test_failed_rollback_is_reported_and_release_stays_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(aws, "LOCAL", tmp_path)
    monkeypatch.setattr(aws, "check_stack", Mock())
    session = Mock()
    session.client.return_value.describe_stacks.return_value = {
        "Stacks": [{"Parameters": []}]
    }
    session.client.return_value.get_template.return_value = {
        "TemplateBody": {"Resources": {}}
    }
    monkeypatch.setattr(
        aws, "deploy_template", Mock(side_effect=RuntimeError("AWS denied deployment"))
    )
    with pytest.raises(RuntimeError, match="SAM deployment failed"):
        aws.release(session)
    receipt = json.loads((tmp_path / "deployment-health.json").read_text())
    assert receipt["status"] == "failed" and receipt["rolled_back"] is False
    assert "AWS denied" in receipt["rollback_error"]


def test_sam_uses_root_configuration_for_nested_templates(monkeypatch):
    run = Mock()
    monkeypatch.setattr(aws.subprocess, "run", run)
    session = Mock(profile_name="hc-studio")
    aws.sam(["deploy", "--template-file", str(aws.BUILD / "template.yaml")], session)
    command = run.call_args.args[0]
    assert command[command.index("--config-file") + 1] == str(
        aws.ROOT / "samconfig.toml"
    )
    assert command[-2:] == ["--profile", "hc-studio"]


@pytest.mark.parametrize(
    "action,replacement,resource_type,blocked",
    [
        ("Modify", "True", "AWS::DynamoDB::Table", True),
        ("Modify", "Conditional", "AWS::Lambda::Function", True),
        ("Remove", None, "AWS::SQS::Queue", True),
        ("Remove", None, "AWS::Lambda::Version", False),
        ("Modify", "False", "AWS::Lambda::Function", False),
    ],
)
def test_change_set_blocks_replacement_and_removal_before_execution(
    tmp_path, monkeypatch, action, replacement, resource_type, blocked
):
    monkeypatch.setattr(aws, "LOCAL", tmp_path)
    client = Mock()
    client.get_paginator.return_value.paginate.return_value = [
        {
            "Changes": [
                {
                    "ResourceChange": {
                        "LogicalResourceId": "Resource",
                        "ResourceType": resource_type,
                        "Action": action,
                        "Replacement": replacement,
                    }
                }
            ]
        }
    ]
    if blocked:
        with pytest.raises(aws.DeploymentBlocked):
            aws.execute_checked_change_set(client, "change-set-id")
        client.execute_change_set.assert_not_called()
    else:
        aws.execute_checked_change_set(client, "change-set-id")
        client.execute_change_set.assert_called_once_with(ChangeSetName="change-set-id")
        client.get_waiter.return_value.wait.assert_called_once()


@pytest.mark.parametrize(
    "detection_status,drift_status,blocked",
    [
        ("DETECTION_COMPLETE", "IN_SYNC", False),
        ("DETECTION_COMPLETE", "DRIFTED", True),
        ("DETECTION_FAILED", "IN_SYNC", True),
    ],
)
def test_stack_preflight_requires_successful_current_drift_detection(
    tmp_path, monkeypatch, detection_status, drift_status, blocked
):
    monkeypatch.setattr(aws, "LOCAL", tmp_path)
    session = Mock()
    client = session.client.return_value
    client.get_stack_policy.return_value = {
        "StackPolicyBody": (aws.ROOT / "aws" / "stack-policy.json").read_text()
    }
    client.detect_stack_drift.return_value = {"StackDriftDetectionId": "detection-id"}
    client.describe_stack_drift_detection_status.return_value = {
        "DetectionStatus": detection_status,
        "StackDriftStatus": drift_status,
    }
    if blocked:
        with pytest.raises(aws.DeploymentBlocked):
            aws.check_stack(session)
    else:
        aws.check_stack(session)
    client.detect_stack_drift.assert_called_once_with(StackName="switchboard")


def test_blocked_release_does_not_roll_back_unexecuted_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(aws, "LOCAL", tmp_path)
    session = Mock()
    session.client.return_value.describe_stacks.return_value = {
        "Stacks": [{"Parameters": []}]
    }
    session.client.return_value.get_template.return_value = {
        "TemplateBody": {"Resources": {}}
    }
    monkeypatch.setattr(
        aws, "check_stack", Mock(side_effect=aws.DeploymentBlocked("drift"))
    )
    deploy = Mock()
    monkeypatch.setattr(aws, "deploy_template", deploy)
    with pytest.raises(aws.DeploymentBlocked):
        aws.release(session)
    deploy.assert_not_called()
    receipt = json.loads((tmp_path / "deployment-health.json").read_text())
    assert receipt["status"] == "blocked" and receipt["rolled_back"] is False


@pytest.mark.parametrize("empty", [False, True])
def test_sam_preparation_selects_only_the_new_change_set(monkeypatch, empty):
    session = Mock()
    client = session.client.return_value
    old_id = "old-pending-change-set"
    new_id = "new-change-set"
    client.get_paginator.return_value.paginate.side_effect = [
        [{"Summaries": [{"ChangeSetId": old_id}]}],
        [{"Summaries": [{"ChangeSetId": old_id}, {"ChangeSetId": new_id}]}],
    ]
    client.describe_change_set.return_value = (
        {
            "Status": "FAILED",
            "StatusReason": "The submitted information didn't contain changes.",
        }
        if empty
        else {"Status": "CREATE_COMPLETE"}
    )
    monkeypatch.setattr(
        aws, "outputs", lambda session: {"DeploymentBucket": "artifacts"}
    )
    sam = Mock()
    monkeypatch.setattr(aws, "sam", sam)
    result = aws.prepare_change_set(session, Path("template.yml"))
    assert result == (None if empty else new_id)
    assert "--no-execute-changeset" in sam.call_args.args[0]
    client.execute_change_set.assert_not_called()


def test_change_set_preparation_failure_is_blocked_before_execution(monkeypatch):
    monkeypatch.setattr(
        aws, "prepare_change_set", Mock(side_effect=RuntimeError("denied"))
    )
    session = Mock()
    with pytest.raises(aws.DeploymentBlocked, match="denied"):
        aws.deploy_template(session, Path("template.yml"))
    session.client.assert_not_called()


def test_missing_stack_policy_blocks_before_drift_detection(monkeypatch):
    session = Mock()
    client = session.client.return_value
    client.get_stack_policy.return_value = {}
    with pytest.raises(aws.DeploymentBlocked, match="stack-policy.json"):
        aws.check_stack(session)
    client.detect_stack_drift.assert_not_called()

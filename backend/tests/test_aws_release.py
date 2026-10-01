"""Releases promote CloudFormation aliases only after successful publication."""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

spec = importlib.util.spec_from_file_location(
    "aws_release", Path(__file__).resolve().parents[2] / "scripts" / "aws.py"
)
assert spec is not None and spec.loader is not None
aws = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aws)


@pytest.mark.parametrize("publication_fails", [False, True])
def test_website_release_preserves_other_components_and_configuration(
    tmp_path, monkeypatch, publication_fails
):
    monkeypatch.setattr(aws, "BUILD", tmp_path)
    monkeypatch.setattr(aws, "LOCAL", tmp_path)
    (tmp_path / "website.zip").write_bytes(b"website package")
    session = Mock()
    cloudformation = Mock()
    functions = Mock()
    session.client.side_effect = lambda name: {
        "cloudformation": cloudformation,
        "lambda": functions,
    }[name]
    parameters = {
        "RuntimeReady": "live",
        "ModelProvider": "bedrock",
        "BedrockModel": "openai.gpt-6-luna",
        "APIVersion": "20",
        "WorkerVersion": "19",
        "ReceiverVersion": "9",
        "WebsiteVersion": "15",
        "AlertEmail": "operator@example.com",
    }
    cloudformation.describe_stacks.return_value = {
        "Stacks": [
            {
                "Parameters": [
                    {"ParameterKey": key, "ParameterValue": value}
                    for key, value in parameters.items()
                ]
            }
        ]
    }
    no_changes = ClientError(
        {
            "Error": {
                "Code": "ValidationError",
                "Message": "No updates are to be performed.",
            }
        },
        "UpdateStack",
    )
    cloudformation.update_stack.side_effect = [no_changes, {}]
    functions.get_alias.return_value = {"FunctionVersion": "15"}
    functions.publish_version.return_value = {"Version": "16"}
    if publication_fails:
        functions.publish_version.side_effect = RuntimeError("Publication failed")
        with pytest.raises(RuntimeError, match="Publication failed"):
            aws.release(session, "website")
    else:
        aws.release(session, "website")

    functions.update_function_code.assert_called_once_with(
        FunctionName="switchboard-website", ZipFile=b"website package"
    )
    functions.update_function_configuration.assert_not_called()
    functions.update_alias.assert_not_called()
    functions.create_alias.assert_not_called()
    calls = cloudformation.update_stack.call_args_list
    assert len(calls) == (1 if publication_fails else 2)
    if not publication_fails:
        promotion = calls[-1].kwargs
        assert promotion["UsePreviousTemplate"] is True
        assert promotion["Parameters"] == [
            {"ParameterKey": key, "UsePreviousValue": True}
            for key in parameters
            if key != "WebsiteVersion"
        ] + [{"ParameterKey": "WebsiteVersion", "ParameterValue": "16"}]


def test_backend_rollback_restores_versions_and_model_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(aws, "LOCAL", tmp_path)
    previous = {"api": "20", "worker": "19", "receiver": "9", "website": "15"}
    (tmp_path / "previous-aliases.json").write_text(json.dumps(previous))
    (tmp_path / "previous-runtime.json").write_text(
        json.dumps({"ModelProvider": "deepseek", "BedrockModel": "deepseek.v3.2"})
    )
    monkeypatch.setattr(sys, "argv", ["aws.py", "rollback", "--component", "backend"])
    session = Mock()
    monkeypatch.setattr(aws.boto3, "Session", Mock(return_value=session))
    update = Mock()
    monkeypatch.setattr(aws, "update_stack", update)

    aws.main()

    update.assert_called_once_with(
        session,
        {
            "APIVersion": "20",
            "WorkerVersion": "19",
            "ReceiverVersion": "9",
            "ModelProvider": "deepseek",
            "BedrockModel": "deepseek.v3.2",
        },
    )
    session.client.assert_not_called()

"""Model credentials are decrypted only by the explicit credential reader."""

from unittest.mock import Mock

from switchboard.aws_clients import aws_client
from switchboard.configuration import deepseek_api_key, runtime_settings
from switchboard.dynamodb import DynamoStore


def test_parameter_settings_and_encrypted_key_are_cached_independently(monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_CONFIG_PARAMETER", "/switchboard/live/runtime")
    monkeypatch.setenv("DEEPSEEK_KEY_PARAMETER", "/switchboard/live/deepseek-api-key")
    client = Mock()
    client.get_parameter.side_effect = [
        {
            "Parameter": {
                "Value": '{"receiver_url":"https://receiver.lambda-url.us-east-1.on.aws/"}'
            }
        },
        {"Parameter": {"Value": "test-secret"}},
    ]
    monkeypatch.setattr("switchboard.configuration.aws_client", lambda name: client)
    assert runtime_settings().receiver_url is not None
    client.get_parameter.assert_called_once_with(Name="/switchboard/live/runtime")
    assert deepseek_api_key() == deepseek_api_key() == "test-secret"
    assert client.get_parameter.call_count == 2
    client.get_parameter.assert_called_with(
        Name="/switchboard/live/deepseek-api-key", WithDecryption=True
    )


def test_connections_are_reused_without_sharing_workspace_state(monkeypatch):
    factory = Mock(return_value=Mock())
    monkeypatch.setattr("switchboard.aws_clients.boto3.client", factory)
    first = DynamoStore("switchboard")
    second = DynamoStore("switchboard")
    assert first.client is second.client is aws_client("dynamodb")
    assert factory.call_count == 1
    first.generations["visitor"] = "generation-one"
    first.staging.add("visitor")
    first.lease = {"id": "first-request"}
    assert second.generations == {}
    assert second.staging == set()
    assert second.lease is None

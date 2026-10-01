"""Model credentials are decrypted only by the explicit credential reader."""

from unittest.mock import Mock

from switchboard.configuration import deepseek_api_key, runtime_settings


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
    monkeypatch.setattr("switchboard.configuration.boto3.client", lambda name: client)
    assert runtime_settings().receiver_url is not None
    client.get_parameter.assert_called_once_with(Name="/switchboard/live/runtime")
    assert deepseek_api_key() == deepseek_api_key() == "test-secret"
    assert client.get_parameter.call_count == 2
    client.get_parameter.assert_called_with(
        Name="/switchboard/live/deepseek-api-key", WithDecryption=True
    )

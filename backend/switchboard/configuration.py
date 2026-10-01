"""Read deployment settings and the model credential from Parameter Store."""

import os
from functools import lru_cache

from pydantic import HttpUrl

from switchboard.aws_clients import aws_client
from switchboard.models import Record, Text


class RuntimeSettings(Record):
    receiver_url: HttpUrl | None = None
    state_machine_arn: Text | None = None


@lru_cache
def runtime_settings() -> RuntimeSettings:
    parameter = os.getenv("SWITCHBOARD_CONFIG_PARAMETER")
    if parameter:
        response = aws_client("ssm").get_parameter(Name=parameter)
        return RuntimeSettings.model_validate_json(response["Parameter"]["Value"])

    return RuntimeSettings.model_validate(
        {
            "receiver_url": os.getenv("SWITCHBOARD_RECEIVER_URL"),
            "state_machine_arn": os.getenv("SWITCHBOARD_STATE_MACHINE_ARN"),
        }
    )


@lru_cache
def deepseek_api_key() -> str | None:
    parameter = os.getenv("DEEPSEEK_KEY_PARAMETER")
    if not parameter:
        return os.getenv("DEEPSEEK_API_KEY")

    response = aws_client("ssm").get_parameter(Name=parameter, WithDecryption=True)
    value = response["Parameter"]["Value"]
    if not value.strip():
        raise ValueError("The model credential is empty")

    return value

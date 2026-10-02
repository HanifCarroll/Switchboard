#!/usr/bin/env python3
"""Run SAM builds and deployments with live checks and application rollback."""

import argparse
import http.cookiejar
import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.request import HTTPCookieProcessor, build_opener, urlopen

import boto3
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / ".aws-sam" / "build"
LOCAL = ROOT / "aws" / "local"


class DeploymentBlocked(RuntimeError):
    """A pre-deployment check rejected changes before execution."""


def check_stack(session):
    """Require database protection and a current, successful drift check."""

    # 1. Verify the checked-in database protection is installed on the stack.
    client = session.client("cloudformation")
    expected = json.loads((ROOT / "aws" / "stack-policy.json").read_text())
    response = client.get_stack_policy(StackName="switchboard")
    installed = json.loads(response.get("StackPolicyBody", "{}"))
    if installed != expected:
        raise DeploymentBlocked("Install aws/stack-policy.json before releasing.")

    # 2. Wait for a fresh drift result, failing closed on errors or timeout.
    detection = client.detect_stack_drift(StackName="switchboard")
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        result = client.describe_stack_drift_detection_status(
            StackDriftDetectionId=detection["StackDriftDetectionId"]
        )
        if result["DetectionStatus"] != "DETECTION_IN_PROGRESS":
            LOCAL.mkdir(parents=True, exist_ok=True)
            (LOCAL / "drift.json").write_text(
                json.dumps(result, indent=2, default=str) + "\n"
            )
            if (
                result["DetectionStatus"] != "DETECTION_COMPLETE"
                or result.get("StackDriftStatus") != "IN_SYNC"
            ):
                raise DeploymentBlocked("Stack drift check failed; inspect drift.json.")
            return

        time.sleep(5)

    raise DeploymentBlocked("Stack drift detection exceeded five minutes.")


def change_set_ids(client):
    return {
        change["ChangeSetId"]
        for page in client.get_paginator("list_change_sets").paginate(
            StackName="switchboard"
        )
        for change in page["Summaries"]
    }


def check_change_set(client, change_set_id):
    """Reject replacements and removals, except retired SAM function versions."""

    # 1. Save every page of the exact change set that will be executed.
    changes = [
        change["ResourceChange"]
        for page in client.get_paginator("describe_change_set").paginate(
            ChangeSetName=change_set_id, IncludePropertyValues=True
        )
        for change in page.get("Changes", [])
    ]
    LOCAL.mkdir(parents=True, exist_ok=True)
    (LOCAL / "change-set.json").write_text(
        json.dumps({"id": change_set_id, "changes": changes}, indent=2) + "\n"
    )

    # 2. Fail before execution when existing resources might be replaced or removed.
    for change in changes:
        removal = (
            change["Action"] == "Remove"
            and change["ResourceType"] != "AWS::Lambda::Version"
        )
        replacement = change.get("Replacement") in {"True", "Conditional"}
        if removal or replacement:
            raise DeploymentBlocked(
                f"Unsafe change to {change['LogicalResourceId']}; inspect change-set.json."
            )


def execute_checked_change_set(client, change_set_id):
    try:
        check_change_set(client, change_set_id)
    except Exception as error:
        raise DeploymentBlocked(str(error)) from error

    client.execute_change_set(ChangeSetName=change_set_id)
    client.get_waiter("stack_update_complete").wait(
        StackName="switchboard", WaiterConfig={"Delay": 5, "MaxAttempts": 120}
    )


def sam(arguments, session):
    command = ["sam", *arguments, "--config-file", str(ROOT / "samconfig.toml")]
    if session.profile_name and session.profile_name != "default":
        command.extend(["--profile", session.profile_name])
    subprocess.run(
        command, cwd=ROOT, check=True, env={**os.environ, "SAM_CLI_TELEMETRY": "0"}
    )


def build(session):
    LOCAL.mkdir(parents=True, exist_ok=True)
    previous_zip = LOCAL / "previous-website.zip"
    previous_zip.unlink(missing_ok=True)
    client = session.client("lambda")
    try:
        previous = client.get_function(
            FunctionName="switchboard-website", Qualifier="live"
        )
    except client.exceptions.ResourceNotFoundException:
        previous = None
    if previous:
        with urlopen(previous["Code"]["Location"], timeout=30) as response:
            previous_zip.write_bytes(response.read())

    subprocess.run(
        ["sam", "build"],
        cwd=ROOT,
        check=True,
        env={**os.environ, "UV_LOCKED": "1", "SAM_CLI_TELEMETRY": "0"},
    )
    subprocess.run(
        ["uv", "run", "python", "../scripts/sam_artifacts.py", "clean"],
        cwd=ROOT / "backend",
        check=True,
    )


def prepare_change_set(session, template, parameters=None):
    client = session.client("cloudformation")
    previous_ids = change_set_ids(client)
    arguments = [
        "deploy",
        "--template-file",
        str(template),
        "--s3-bucket",
        outputs(session)["DeploymentBucket"],
        "--no-execute-changeset",
    ]
    if parameters:
        arguments.extend(
            [
                "--parameter-overrides",
                *[f"{key}={value}" for key, value in parameters.items()],
            ]
        )
    sam(arguments, session)
    created_ids = change_set_ids(client) - previous_ids
    if len(created_ids) != 1:
        raise DeploymentBlocked("Cannot identify a unique SAM change set.")

    change_set_id = created_ids.pop()
    result = client.describe_change_set(ChangeSetName=change_set_id)
    if result["Status"] == "FAILED":
        reason = result.get("StatusReason", "")
        if (
            "The submitted information didn't contain changes." in reason
            or "No updates are to be performed" in reason
        ):
            return None

        raise DeploymentBlocked(f"SAM change set failed: {reason}")

    if result["Status"] != "CREATE_COMPLETE":
        raise DeploymentBlocked("SAM change set is not ready for inspection.")

    return change_set_id


def deploy_template(session, template, parameters=None):
    try:
        change_set_id = prepare_change_set(session, template, parameters)
    except Exception as error:
        raise DeploymentBlocked(str(error)) from error

    if change_set_id is not None:
        execute_checked_change_set(session.client("cloudformation"), change_set_id)


def rollback(session):
    previous = json.loads((LOCAL / "previous-deployment.json").read_text())
    deploy_template(session, LOCAL / "previous-template.yml", previous["parameters"])
    smoke_check(session)
    print("Previous application and runtime settings restored through CloudFormation.")


def release(session, model_provider=None, bedrock_model=None, alert_email=None):

    # 1. Save the deployable template and all current runtime parameters.
    LOCAL.mkdir(parents=True, exist_ok=True)
    client = session.client("cloudformation")
    stack = client.describe_stacks(StackName="switchboard")["Stacks"][0]
    settings = {
        item["ParameterKey"]: item["ParameterValue"] for item in stack["Parameters"]
    }
    body = client.get_template(StackName="switchboard", TemplateStage="Original")[
        "TemplateBody"
    ]
    (LOCAL / "previous-template.yml").write_text(
        body if isinstance(body, str) else json.dumps(body, indent=2) + "\n"
    )
    (LOCAL / "previous-deployment.json").write_text(
        json.dumps({"parameters": settings}, indent=2) + "\n"
    )
    changes = {}
    if model_provider:
        changes["ModelProvider"] = model_provider
    if bedrock_model:
        changes["BedrockModel"] = bedrock_model
    if alert_email is not None:
        changes["AlertEmail"] = alert_email

    # 2. Let SAM package the code and CloudFormation publish the live aliases.
    receipt = {"status": "pending", "rolled_back": False}
    try:
        check_stack(session)
    except Exception as error:
        receipt.update(status="blocked", reason=str(error))
        (LOCAL / "deployment-health.json").write_text(
            json.dumps(receipt, indent=2) + "\n"
        )
        raise DeploymentBlocked(str(error)) from error

    try:
        deploy_template(session, BUILD / "template.yaml", changes)
        smoke_check(session)
    except DeploymentBlocked as error:
        receipt.update(status="blocked", reason=str(error))
        (LOCAL / "deployment-health.json").write_text(
            json.dumps(receipt, indent=2) + "\n"
        )
        raise
    except Exception as error:
        receipt.update(status="failed", reason=str(error))
        try:
            rollback(session)
            receipt["rolled_back"] = True
        except Exception as rollback_error:
            receipt["rollback_error"] = str(rollback_error)
        (LOCAL / "deployment-health.json").write_text(
            json.dumps(receipt, indent=2) + "\n"
        )
        raise RuntimeError(
            "SAM deployment failed; inspect deployment-health.json"
        ) from error

    # 3. Record the verified website, asset, API, and database outcome.
    receipt["status"] = "passed"
    (LOCAL / "deployment-health.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print("SAM deployment and live application checks passed.")


def outputs(session):
    response = session.client("cloudformation").describe_stacks(StackName="switchboard")
    return {
        item["OutputKey"]: item["OutputValue"]
        for item in response["Stacks"][0]["Outputs"]
    }


def smoke_check(session):
    """Check deployed HTML, its assets, and database-backed visitor routes."""
    url = outputs(session)["DeliveryURL"].rstrip("/")
    opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
    for attempt in range(3):
        try:
            with opener.open(url + "/", timeout=30) as response:
                html = response.read().decode()
                if response.status != 200 or "Switchboard" not in html:
                    raise RuntimeError("The deployed website is unavailable")
            asset = re.search(r'src="(/_next/static/[^"<>]+\.js)"', html)
            if asset is None:
                raise RuntimeError("The deployed website has no JavaScript bundle")
            with opener.open(url + asset.group(1), timeout=30) as response:
                if response.status != 200 or not response.read():
                    raise RuntimeError("The deployed website bundle is unavailable")
            with opener.open(url + "/api/me", timeout=30) as response:
                identity = json.load(response)
                if identity.get("employee_id") != "emp-alex":
                    raise RuntimeError("The deployed API cannot open a demo workspace")
            with opener.open(url + "/api/demo/personas", timeout=30) as response:
                personas = json.load(response)
                if not any(persona["id"] == "emp-priya" for persona in personas):
                    raise RuntimeError("The deployed API cannot read workspace data")
            return
        except Exception:
            if attempt == 2:
                raise
            time.sleep(3 * (attempt + 1))


def provision_model_key(session):
    """SecureString is provisioned separately because CloudFormation lacks that type."""
    name = "/switchboard/live/deepseek-api-key"
    client = session.client("ssm")
    try:
        client.get_parameter(Name=name)
        return
    except client.exceptions.ParameterNotFound:
        pass
    value = os.getenv("DEEPSEEK_API_KEY") or dotenv_values(
        ROOT / "backend" / ".env"
    ).get("DEEPSEEK_API_KEY")
    if not value:
        configuration = session.client("lambda").get_function_configuration(
            FunctionName="switchboard-worker", Qualifier="live"
        )
        value = (
            configuration.get("Environment", {})
            .get("Variables", {})
            .get("DEEPSEEK_API_KEY")
        )
    if not value:
        raise RuntimeError(
            "A model credential is required to provision Parameter Store"
        )
    client.put_parameter(
        Name=name,
        Value=value,
        Type="SecureString",
        Tier="Standard",
        KeyId="alias/aws/ssm",
    )
    print("Encrypted model credential stored in Parameter Store.")


def connect_jobs(session):
    values = outputs(session)
    mappings = session.client("lambda").list_event_source_mappings(
        FunctionName=values["WorkerArn"] + ":live"
    )["EventSourceMappings"]
    schedule = session.client("scheduler").get_schedule(Name="switchboard-maintenance")
    if (
        not mappings
        or mappings[0]["State"] != "Enabled"
        or schedule["State"] != "ENABLED"
    ):
        raise RuntimeError(
            "CloudFormation worker or maintenance connection is unavailable"
        )
    print("CloudFormation worker and maintenance connections verified.")


def delivery(session):
    values = outputs(session)
    subscriptions = session.client("pricing-plan-manager").list_subscriptions()[
        "subscriptionSummaries"
    ]
    plan = next(
        item for item in subscriptions if item["arn"] == values["DeliveryPlanArn"]
    )
    if plan["planTier"] != "FREE" or plan["status"] != "ACTIVE":
        raise RuntimeError(
            "The CloudFront subscription is not the expected active plan"
        )
    config = session.client("cloudfront").get_distribution_config(
        Id=values["DeliveryId"]
    )["DistributionConfig"]
    if not config["Enabled"]:
        raise RuntimeError("CloudFormation delivery is disabled")
    print(values["DeliveryURL"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=[
            "build",
            "release",
            "rollback",
            "check",
            "jobs",
            "delivery",
            "model-key",
        ],
    )
    parser.add_argument("--profile", default=os.getenv("AWS_PROFILE"))
    parser.add_argument("--model-provider", choices=["bedrock", "deepseek"])
    parser.add_argument(
        "--bedrock-model",
        choices=["deepseek.v3.2", "openai.gpt-6-luna", "minimax.minimax-m2.5"],
    )
    parser.add_argument("--alert-email", help="SNS recipient; confirmation is required")
    arguments = parser.parse_args()
    if arguments.bedrock_model and arguments.model_provider != "bedrock":
        parser.error("--bedrock-model requires --model-provider bedrock")
    session = boto3.Session(profile_name=arguments.profile, region_name="us-east-1")
    if arguments.action == "build":
        build(session)
    elif arguments.action == "release":
        release(
            session,
            arguments.model_provider,
            arguments.bedrock_model,
            arguments.alert_email,
        )
    elif arguments.action == "rollback":
        rollback(session)
    elif arguments.action == "check":
        smoke_check(session)
        print("Live website, assets, identity, and database checks passed.")
    elif arguments.action == "jobs":
        connect_jobs(session)
    elif arguments.action == "delivery":
        delivery(session)
    else:
        provision_model_key(session)


if __name__ == "__main__":
    main()

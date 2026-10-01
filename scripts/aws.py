#!/usr/bin/env python3
"""Build and release Switchboard's Lambda functions and CloudFront delivery."""

import argparse
import hashlib
import http.cookiejar
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from urllib.request import HTTPCookieProcessor, build_opener, urlopen

import boto3
from botocore.exceptions import ClientError
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / ".aws-build"
LOCAL = ROOT / "aws" / "local"
TRACE_LAYER_BYTES = 59807429


def run(arguments, directory, env=None):
    subprocess.run(arguments, cwd=directory, env=env, check=True)


def package(directory, destination, layer_bytes=0):
    files = [
        path
        for path in directory.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    ]
    unpacked = sum(path.stat().st_size for path in files)
    with zipfile.ZipFile(
        destination, "w", zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for path in files:
            archive.write(path, path.relative_to(directory))
    compressed = destination.stat().st_size
    if compressed >= 50 * 1024**2 or unpacked + layer_bytes >= 250 * 1024**2:
        raise RuntimeError(f"Direct-upload limit exceeded: {destination.name}")
    print(
        f"{destination.name}: {compressed / 1024**2:.1f} MiB ZIP; {(unpacked + layer_bytes) / 1024**2:.1f} MiB including layers"
    )
    return {
        "zip_bytes": compressed,
        "unpacked_bytes_with_layers": unpacked + layer_bytes,
        "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
    }


def build(component, session):
    BUILD.mkdir(exist_ok=True)
    receipts = {}

    # 1. Build production Python dependencies for Lambda's Linux runtime.
    if component in {"backend", "all"}:
        directory = BUILD / "backend"
        shutil.rmtree(directory, ignore_errors=True)
        run(
            [
                "uv",
                "export",
                "--frozen",
                "--no-dev",
                "--no-emit-project",
                "--no-hashes",
                "--quiet",
                "--output-file",
                str(BUILD / "requirements.txt"),
            ],
            ROOT / "backend",
        )
        run(
            [
                "uv",
                "pip",
                "install",
                "--python-platform",
                "x86_64-manylinux_2_28",
                "--python-version",
                "3.12",
                "--only-binary",
                ":all:",
                "--target",
                str(directory),
                "--requirements",
                str(BUILD / "requirements.txt"),
            ],
            ROOT / "backend",
        )
        shutil.copytree(
            ROOT / "backend" / "switchboard",
            directory / "switchboard",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        for name in ["fixtures", "scenarios"]:
            shutil.copytree(ROOT / "backend" / "data" / name, directory / "data" / name)
        # Ship only the AWS service definitions used by the application.
        services = {
            "bedrock",
            "bedrock-runtime",
            "dynamodb",
            "sqs",
            "ssm",
            "stepfunctions",
            "sts",
            "xray",
        }
        for service in (directory / "botocore" / "data").iterdir():
            if service.is_dir() and service.name not in services:
                shutil.rmtree(service)
        if sys.platform.startswith("linux"):
            run(
                [
                    "uv",
                    "run",
                    "--no-project",
                    "--python",
                    "3.12",
                    "python",
                    "-c",
                    (
                        "from switchboard.lambda_handler import handler; "
                        "from switchboard.jobs import worker_handler; "
                        "from switchboard.receiver import handler as receiver; "
                        "from switchboard.workflow import handle; "
                        "from switchboard.investigation.agent import create_model; "
                        "import boto3; "
                        "[boto3.client(name) for name in ('dynamodb', 'sqs', 'ssm', 'stepfunctions', 'xray', 'sts')]; "
                        "assert create_model().model_name == 'deepseek.v3.2'"
                    ),
                ],
                directory,
                {
                    **os.environ,
                    "PYTHONPATH": str(directory),
                    "AWS_ACCESS_KEY_ID": "package-check",
                    "AWS_SECRET_ACCESS_KEY": "package-check",
                    "AWS_DEFAULT_REGION": "us-east-1",
                    "SWITCHBOARD_MODEL_PROVIDER": "bedrock",
                    "SWITCHBOARD_BEDROCK_MODEL": "deepseek.v3.2",
                },
            )
        receipts["backend"] = package(
            directory, BUILD / "backend.zip", layer_bytes=TRACE_LAYER_BYTES
        )

    # 2. Bundle Next.js and retain one release of static assets.
    if component in {"website", "all"}:
        env = {
            **os.environ,
            "SWITCHBOARD_AWS": "1",
            "NEXT_PUBLIC_AWS_DEPLOYMENT": "1",
            "NEXT_PUBLIC_AUTH_MODE": "demo",
        }
        run(["npm", "run", "build"], ROOT / "frontend", env)
        directory = BUILD / "website"
        shutil.rmtree(directory, ignore_errors=True)
        shutil.copytree(
            ROOT / "frontend" / ".next" / "standalone",
            directory,
            ignore=shutil.ignore_patterns(".env*"),
        )
        shutil.copytree(
            ROOT / "frontend" / "public", directory / "public", dirs_exist_ok=True
        )
        shutil.copytree(
            ROOT / "frontend" / ".next" / "static",
            directory / ".next" / "static",
            dirs_exist_ok=True,
        )
        current_assets = [
            str(path.relative_to(directory))
            for path in (directory / ".next" / "static").rglob("*")
            if path.is_file()
        ]
        (directory / "release-static.json").write_text(json.dumps(current_assets))
        # Retain the previous published release's fingerprinted assets.
        client = session.client("lambda")
        try:
            previous = client.get_function(
                FunctionName="switchboard-website", Qualifier="live"
            )
        except client.exceptions.ResourceNotFoundException:
            previous = None
        if previous:
            with urlopen(previous["Code"]["Location"], timeout=30) as response:
                content = response.read()
            previous_zip = BUILD / "previous-website.zip"
            previous_zip.write_bytes(content)
            with zipfile.ZipFile(previous_zip) as archive:
                retained = (
                    set(json.loads(archive.read("release-static.json")))
                    if "release-static.json" in archive.namelist()
                    else set(archive.namelist())
                )
                for entry in archive.infolist():
                    if (
                        entry.filename.startswith(".next/static/")
                        and entry.filename in retained
                        and not entry.is_dir()
                        and ".." not in Path(entry.filename).parts
                    ):
                        target = directory / entry.filename
                        if not target.exists():
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_bytes(archive.read(entry))
        script = directory / "run.sh"
        script.write_text(
            "#!/bin/sh\nexport HOSTNAME=0.0.0.0\nexport PORT=8080\nexec node /var/task/server.js\n"
        )
        script.chmod(
            stat.S_IRUSR
            | stat.S_IWUSR
            | stat.S_IXUSR
            | stat.S_IRGRP
            | stat.S_IXGRP
            | stat.S_IROTH
            | stat.S_IXOTH
        )
        # The pinned adapter has <6 MiB uncompressed; keep a conservative layer budget.
        receipts["website"] = package(
            directory, BUILD / "website.zip", layer_bytes=6 * 1024**2
        )

    # 3. Save package sizes and hashes without discarding other components.
    LOCAL.mkdir(parents=True, exist_ok=True)
    receipt_path = LOCAL / "packaging.json"
    previous_receipts = (
        json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
    )
    receipt_path.write_text(
        json.dumps({**previous_receipts, **receipts}, indent=2) + "\n"
    )


def provision(session, alert_email=None):
    provision_model_key(session)
    client = session.client("cloudformation")
    concurrency = session.client("lambda").get_account_settings()["AccountLimit"][
        "ConcurrentExecutions"
    ]
    reservations = "enabled" if concurrency >= 122 else "disabled"
    arguments = {
        "StackName": "switchboard",
        "TemplateBody": json.dumps(
            json.loads((ROOT / "aws" / "template.json").read_text()),
            separators=(",", ":"),
        ),
        "Capabilities": ["CAPABILITY_NAMED_IAM"],
    }
    try:
        client.describe_stacks(StackName="switchboard")
    except client.exceptions.ClientError as error:
        if "does not exist" not in str(error):
            raise
        arguments["Parameters"] = [
            {"ParameterKey": "ReserveConcurrency", "ParameterValue": reservations}
        ]
        if alert_email is not None:
            arguments["Parameters"].append(
                {"ParameterKey": "AlertEmail", "ParameterValue": alert_email}
            )
        client.create_stack(**arguments, EnableTerminationProtection=True)
        waiter = "stack_create_complete"
    else:
        changes = {"ReserveConcurrency": reservations}
        if alert_email is not None:
            changes["AlertEmail"] = alert_email
        update_stack(session, changes, template=arguments["TemplateBody"])
        client.update_termination_protection(
            StackName="switchboard", EnableTerminationProtection=True
        )
        return
    print(
        "Provisioning fixed-capacity storage, queues, Lambda functions, and execution roles.",
        flush=True,
    )
    client.get_waiter(waiter).wait(
        StackName="switchboard", WaiterConfig={"Delay": 5, "MaxAttempts": 120}
    )


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


def release(session, component, mode="live", model_provider=None, bedrock_model=None):
    # 1. Preserve live versions before uploading code or changing configuration.
    if mode != "live":
        raise ValueError("Hosted fixture execution is disabled")
    stack = session.client("cloudformation").describe_stacks(StackName="switchboard")[
        "Stacks"
    ][0]
    settings = {
        item["ParameterKey"]: item["ParameterValue"] for item in stack["Parameters"]
    }
    if settings.get("RuntimeReady", "bootstrap") != "live" and component != "all":
        raise ValueError("The first application release must include all components")
    if (model_provider or bedrock_model) and component == "website":
        raise ValueError("Model selection requires a backend release")
    names = (
        ["receiver", "worker", "api"]
        if component == "backend"
        else ["website"]
        if component == "website"
        else ["receiver", "worker", "api", "website"]
    )
    client = session.client("lambda")
    LOCAL.mkdir(parents=True, exist_ok=True)
    receipt_path = LOCAL / "previous-aliases.json"
    previous = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
    for name in names:
        previous[name] = client.get_alias(
            FunctionName=f"switchboard-{name}", Name="live"
        )["FunctionVersion"]
    receipt_path.write_text(json.dumps(previous, indent=2) + "\n")
    if "worker" in names:
        (LOCAL / "previous-runtime.json").write_text(
            json.dumps(
                {key: settings[key] for key in ("ModelProvider", "BedrockModel")},
                indent=2,
            )
            + "\n"
        )

    # 2. Upload each ZIP while live aliases continue serving immutable versions.
    for name in names:
        function = f"switchboard-{name}"
        client.update_function_code(
            FunctionName=function,
            ZipFile=(
                BUILD / ("website.zip" if name == "website" else "backend.zip")
            ).read_bytes(),
        )
        client.get_waiter("function_updated_v2").wait(FunctionName=function)

    # 3. Apply CloudFormation runtime settings before publishing new versions.
    changes = {"RuntimeReady": "live"}
    if model_provider:
        changes["ModelProvider"] = model_provider
    if bedrock_model:
        changes["BedrockModel"] = bedrock_model
    update_stack(session, changes)
    versions = {}
    for name in names:
        version = client.publish_version(FunctionName=f"switchboard-{name}")["Version"]
        versions[("API" if name == "api" else name.title()) + "Version"] = version
        print(f"Published {name} version {version}.", flush=True)

    # 4. Promote and verify; restore the previous selection if health checks fail.
    receipt = {"status": "pending", "components": names}
    try:
        update_stack(session, versions)
        smoke_check(session)
    except Exception as error:
        receipt.update(status="failed", reason=str(error), rolled_back=False)
        rollback_changes = {key: settings[key] for key in versions}
        rollback_changes.update({key: settings[key] for key in changes})
        try:
            update_stack(session, rollback_changes)
            receipt["rolled_back"] = True
            print("Deployment checks failed; previous versions restored.", flush=True)
        finally:
            (LOCAL / "deployment-health.json").write_text(
                json.dumps(receipt, indent=2) + "\n"
            )
        raise RuntimeError("Deployment failed its health checks") from error
    receipt["status"] = "passed"
    (LOCAL / "deployment-health.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print("Deployed website, static assets, API identity, and storage checks passed.")


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


def update_stack(session, changes=None, template=None):
    """Preserve unrelated stack parameters for configuration, release, and rollback."""
    client = session.client("cloudformation")
    stack = client.describe_stacks(StackName="switchboard")["Stacks"][0]
    changes = changes or {}
    parameters = [
        {"ParameterKey": item["ParameterKey"], "UsePreviousValue": True}
        for item in stack.get("Parameters", [])
        if item["ParameterKey"] not in changes
    ]
    parameters.extend(
        {"ParameterKey": key, "ParameterValue": value} for key, value in changes.items()
    )
    arguments = {
        "StackName": "switchboard",
        "Parameters": parameters,
        "Capabilities": ["CAPABILITY_NAMED_IAM"],
    }
    if template is None:
        arguments["UsePreviousTemplate"] = True
    else:
        arguments["TemplateBody"] = template
    try:
        client.update_stack(**arguments)
    except ClientError as error:
        if "No updates" in str(error):
            return
        raise
    client.get_waiter("stack_update_complete").wait(
        StackName="switchboard", WaiterConfig={"Delay": 5, "MaxAttempts": 120}
    )


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
        choices=["build", "provision", "release", "jobs", "delivery", "rollback"],
    )
    parser.add_argument(
        "--component", choices=["all", "backend", "website"], default="all"
    )
    parser.add_argument("--profile", default=os.getenv("AWS_PROFILE"))
    parser.add_argument("--alert-email", help="SNS alert recipient (provision only)")
    parser.add_argument(
        "--model-provider",
        choices=["bedrock", "deepseek"],
        help="Worker provider (release only)",
    )
    parser.add_argument(
        "--bedrock-model",
        choices=["deepseek.v3.2", "openai.gpt-6-luna"],
        help="Bedrock worker model (release with --model-provider bedrock)",
    )
    arguments = parser.parse_args()
    session = boto3.Session(profile_name=arguments.profile, region_name="us-east-1")
    if arguments.action == "build":
        build(arguments.component, session)
    elif arguments.action == "provision":
        provision(session, arguments.alert_email)
    elif arguments.action == "release":
        if arguments.bedrock_model and arguments.model_provider != "bedrock":
            parser.error("--bedrock-model requires --model-provider bedrock")
        release(
            session,
            arguments.component,
            model_provider=arguments.model_provider,
            bedrock_model=arguments.bedrock_model,
        )
    elif arguments.action == "jobs":
        connect_jobs(session)
    elif arguments.action == "delivery":
        delivery(session)
    else:
        previous = json.loads((LOCAL / "previous-aliases.json").read_text())
        names = (
            {"api", "worker", "receiver"}
            if arguments.component == "backend"
            else {"website"}
            if arguments.component == "website"
            else set(previous)
        )
        changes = {
            ("API" if name == "api" else name.title()) + "Version": version
            for name, version in previous.items()
            if name in names
        }
        runtime_path = LOCAL / "previous-runtime.json"
        if "worker" in names and runtime_path.exists():
            changes.update(json.loads(runtime_path.read_text()))
        update_stack(session, changes)
        for name, version in previous.items():
            if name in names:
                print(f"Restored {name} version {version}.")


if __name__ == "__main__":
    main()

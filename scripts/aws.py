#!/usr/bin/env python3
"""Build and release Switchboard's Lambda functions and CloudFront delivery."""

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.request import urlopen

import boto3
from botocore.exceptions import ClientError
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / ".aws-build"
LOCAL = ROOT / "aws" / "local"
LAYER = "arn:aws:lambda:us-east-1:753240598075:layer:LambdaAdapterLayerX86:30"
TRACE_LAYER = (
    "arn:aws:lambda:us-east-1:901920570463:layer:aws-otel-python-amd64-ver-1-32-0:7"
)
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
    arguments = {
        "StackName": "switchboard",
        "TemplateBody": json.dumps(
            json.loads((ROOT / "aws" / "template.json").read_text())
        ),
        "Capabilities": ["CAPABILITY_NAMED_IAM"],
    }
    try:
        existing = client.describe_stacks(StackName="switchboard")["Stacks"][0]
    except client.exceptions.ClientError as error:
        if "does not exist" not in str(error):
            raise
        if alert_email is not None:
            arguments["Parameters"] = [
                {"ParameterKey": "AlertEmail", "ParameterValue": alert_email}
            ]
        client.create_stack(**arguments)
        waiter = "stack_create_complete"
    else:
        arguments["Parameters"] = [
            {"ParameterKey": item["ParameterKey"], "UsePreviousValue": True}
            for item in existing.get("Parameters", [])
        ]
        if alert_email is not None:
            arguments["Parameters"] = [
                item
                for item in arguments["Parameters"]
                if item["ParameterKey"] != "AlertEmail"
            ] + [{"ParameterKey": "AlertEmail", "ParameterValue": alert_email}]
        try:
            client.update_stack(**arguments)
        except ClientError as error:
            if "No updates" in str(error):
                return
            raise
        waiter = "stack_update_complete"
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


def release(session, component, mode="live", model_provider=None, bedrock_model=None):
    # 1. Resolve runtime settings and preserve rollback information before changes.
    values = outputs(session)
    client = session.client("lambda")
    LOCAL.mkdir(parents=True, exist_ok=True)
    receipt_path = LOCAL / "previous-aliases.json"
    previous = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
    common = {
        "DYNAMODB_TABLE": values["Database"],
        "SWITCHBOARD_AUTH_MODE": "demo",
        "SWITCHBOARD_RUNTIME": "aws",
        "SWITCHBOARD_INVESTIGATION_MODE": mode,
        "LANGCHAIN_TRACING_V2": "false",
        "LANGSMITH_TRACING": "false",
    }
    if mode != "live":
        raise ValueError("Hosted fixture execution is disabled")
    # 2. Publish immutable versions; each component keeps its own live alias.
    for name in (
        ["receiver", "worker", "api"]
        if component == "backend"
        else ["website"]
        if component == "website"
        else ["receiver", "worker", "api", "website"]
    ):
        function = f"switchboard-{name}"
        try:
            previous[name] = client.get_alias(FunctionName=function, Name="live")[
                "FunctionVersion"
            ]
        except client.exceptions.ResourceNotFoundException:
            previous.pop(name, None)
        receipt_path.write_text(json.dumps(previous, indent=2) + "\n")
        env = dict(common)
        env.update(
            AWS_LAMBDA_EXEC_WRAPPER="/opt/otel-instrument",
            OTEL_SERVICE_NAME=function,
            OTEL_METRICS_EXPORTER="none",
            OTEL_LOGS_EXPORTER="none",
            OTEL_PYTHON_DISABLED_INSTRUMENTATIONS="fastapi,httpx,requests,urllib,urllib3",
        )
        if name in {"api", "worker"}:
            env["SWITCHBOARD_CONFIG_PARAMETER"] = values["RuntimeParameter"]
        env["INVESTIGATION_QUEUE_URL"] = values["Investigations"]
        if name == "api":
            env["INVESTIGATION_DLQ_URL"] = values["DeadLetters"]
        if name == "worker":
            try:
                current_configuration = client.get_function_configuration(
                    FunctionName=function, Qualifier="live"
                )
            except client.exceptions.ResourceNotFoundException:
                current_configuration = client.get_function_configuration(
                    FunctionName=function
                )
            current_env = current_configuration.get("Environment", {}).get(
                "Variables", {}
            )
            provider = model_provider or current_env.get(
                "SWITCHBOARD_MODEL_PROVIDER", "deepseek"
            )
            env["SWITCHBOARD_MODEL_PROVIDER"] = provider
            if provider == "bedrock":
                env["SWITCHBOARD_BEDROCK_MODEL"] = bedrock_model or current_env.get(
                    "SWITCHBOARD_BEDROCK_MODEL", "deepseek.v3.2"
                )
            if provider == "deepseek":
                env["DEEPSEEK_KEY_PARAMETER"] = values["ModelKeyParameter"]
        if name == "website":
            env = {
                "AWS_LAMBDA_EXEC_WRAPPER": "/opt/bootstrap",
                "AWS_LWA_PORT": "8080",
                "AWS_LWA_READINESS_CHECK_PATH": "/work",
                "AWS_LWA_READINESS_CHECK_HEALTHY_STATUS": "200-399",
                "AWS_LWA_ENABLE_COMPRESSION": "true",
                "AWS_LWA_INVOKE_MODE": "buffered",
                "NEXT_PUBLIC_AUTH_MODE": "demo",
            }
        client.update_function_configuration(
            FunctionName=function,
            Handler="run.sh"
            if name == "website"
            else "switchboard.jobs.worker_handler"
            if name == "worker"
            else "switchboard.receiver.handler"
            if name == "receiver"
            else "switchboard.lambda_handler.handler",
            Environment={"Variables": env},
            Layers=[LAYER] if name == "website" else [TRACE_LAYER],
            TracingConfig={"Mode": "Active"},
        )
        client.get_waiter("function_updated_v2").wait(FunctionName=function)
        client.update_function_code(
            FunctionName=function,
            ZipFile=(
                BUILD / ("website.zip" if name == "website" else "backend.zip")
            ).read_bytes(),
        )
        client.get_waiter("function_updated_v2").wait(FunctionName=function)
        version = client.publish_version(FunctionName=function)["Version"]
        if name == "receiver":
            promote_receiver(session, version)
        elif name in previous:
            client.update_alias(
                FunctionName=function, Name="live", FunctionVersion=version
            )
        else:
            client.create_alias(
                FunctionName=function, Name="live", FunctionVersion=version
            )
        print(f"Published {name} version {version}.", flush=True)


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


def promote_receiver(session, version):
    """Change the CloudFormation-owned alias without changing infrastructure."""
    client = session.client("cloudformation")
    stack = client.describe_stacks(StackName="switchboard")["Stacks"][0]
    parameters = [
        {"ParameterKey": item["ParameterKey"], "UsePreviousValue": True}
        for item in stack.get("Parameters", [])
        if item["ParameterKey"] != "ReceiverVersion"
    ]
    parameters.append({"ParameterKey": "ReceiverVersion", "ParameterValue": version})
    client.update_stack(
        StackName="switchboard",
        UsePreviousTemplate=True,
        Parameters=parameters,
        Capabilities=["CAPABILITY_NAMED_IAM"],
    )
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
        for name, version in previous.items():
            if name not in names:
                continue
            if name == "receiver":
                promote_receiver(session, version)
            else:
                session.client("lambda").update_alias(
                    FunctionName=f"switchboard-{name}",
                    Name="live",
                    FunctionVersion=version,
                )
            print(f"Restored {name} version {version}.")


if __name__ == "__main__":
    main()

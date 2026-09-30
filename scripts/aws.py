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
        services = {"bedrock", "bedrock-runtime", "dynamodb", "sqs", "sts", "xray"}
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
                    "from switchboard.lambda_handler import handler; "
                    "from switchboard.jobs import worker_handler; "
                    "from switchboard.investigation.agent import create_model; "
                    "import boto3; "
                    "[boto3.client(name) for name in ('dynamodb', 'sqs', 'xray', 'sts')]; "
                    "assert create_model().model_id == 'deepseek.v3.2'",
                ],
                directory,
                {
                    **os.environ,
                    "PYTHONPATH": str(directory),
                    "AWS_ACCESS_KEY_ID": "package-check",
                    "AWS_SECRET_ACCESS_KEY": "package-check",
                    "AWS_DEFAULT_REGION": "us-east-1",
                    "SWITCHBOARD_MODEL_PROVIDER": "bedrock",
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
    client = session.client("cloudformation")
    arguments = {
        "StackName": "switchboard",
        "TemplateBody": (ROOT / "aws" / "template.json").read_text(),
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
        if alert_email is not None:
            arguments["Parameters"] = [
                {"ParameterKey": "AlertEmail", "ParameterValue": alert_email}
            ]
        elif any(
            item["ParameterKey"] == "AlertEmail"
            for item in existing.get("Parameters", [])
        ):
            arguments["Parameters"] = [
                {"ParameterKey": "AlertEmail", "UsePreviousValue": True}
            ]
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


def release(session, component, mode="live", model_provider=None):
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
    model_key = os.getenv("DEEPSEEK_API_KEY") or dotenv_values(
        ROOT / "backend" / ".env"
    ).get("DEEPSEEK_API_KEY")

    # 2. Publish immutable versions; each component keeps its own live alias.
    for name in (
        ["api", "worker"]
        if component == "backend"
        else ["website"]
        if component == "website"
        else ["api", "worker", "website"]
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
        env["INVESTIGATION_QUEUE_URL"] = values["Investigations"]
        if name == "api":
            env["INVESTIGATION_DLQ_URL"] = values["DeadLetters"]
        if name == "worker":
            current_env = (
                client.get_function_configuration(FunctionName=function)
                .get("Environment", {})
                .get("Variables", {})
            )
            provider = model_provider or current_env.get(
                "SWITCHBOARD_MODEL_PROVIDER", "deepseek"
            )
            env["SWITCHBOARD_MODEL_PROVIDER"] = provider
            if not model_key:
                model_key = current_env.get("DEEPSEEK_API_KEY")
            if provider == "deepseek" and not model_key:
                raise RuntimeError(
                    "DEEPSEEK_API_KEY is required for initial worker deployment"
                )
            if provider == "deepseek":
                env["DEEPSEEK_API_KEY"] = model_key
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
        if name in previous:
            client.update_alias(
                FunctionName=function, Name="live", FunctionVersion=version
            )
        else:
            client.create_alias(
                FunctionName=function, Name="live", FunctionVersion=version
            )
        print(f"Published {name} version {version}.", flush=True)


def connect_jobs(session):
    values = outputs(session)
    client = session.client("lambda")
    target = values["WorkerArn"] + ":live"
    mappings = client.list_event_source_mappings(
        FunctionName=target,
        EventSourceArn=session.client("sqs").get_queue_attributes(
            QueueUrl=values["Investigations"], AttributeNames=["QueueArn"]
        )["Attributes"]["QueueArn"],
    )["EventSourceMappings"]
    if not mappings:
        source = session.client("sqs").get_queue_attributes(
            QueueUrl=values["Investigations"], AttributeNames=["QueueArn"]
        )["Attributes"]["QueueArn"]
        client.create_event_source_mapping(
            EventSourceArn=source,
            FunctionName=target,
            BatchSize=1,
            MaximumBatchingWindowInSeconds=0,
            ScalingConfig={"MaximumConcurrency": 2},
            Enabled=True,
        )
    scheduler = session.client("scheduler")
    arguments = {
        "Name": "switchboard-maintenance",
        "ScheduleExpression": "rate(1 hour)",
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "State": "ENABLED",
        "Target": {
            "Arn": values["APIArn"] + ":live",
            "RoleArn": values["SchedulerRoleArn"],
            "Input": '{"source":"switchboard.maintenance"}',
            "RetryPolicy": {
                "MaximumRetryAttempts": 2,
                "MaximumEventAgeInSeconds": 3600,
            },
        },
    }
    try:
        scheduler.get_schedule(Name=arguments["Name"])
    except scheduler.exceptions.ResourceNotFoundException:
        scheduler.create_schedule(**arguments)
    else:
        scheduler.update_schedule(**arguments)
    print("Connected SQS worker and hourly maintenance.")


def delivery(session):
    lambdas = session.client("lambda")
    cloudfront = session.client("cloudfront")
    pricing = session.client("pricing-plan-manager")
    waf = session.client("wafv2")
    origins = []
    for name in ["api", "website"]:
        function = f"switchboard-{name}"
        try:
            response = lambdas.get_function_url_config(
                FunctionName=function, Qualifier="live"
            )
        except lambdas.exceptions.ResourceNotFoundException:
            response = lambdas.create_function_url_config(
                FunctionName=function,
                Qualifier="live",
                AuthType="AWS_IAM",
                InvokeMode="BUFFERED",
            )
        if response["AuthType"] != "AWS_IAM":
            raise RuntimeError("Origins must require AWS IAM")
        origins.append(
            {
                "Id": name,
                "DomainName": response["FunctionUrl"].split("/")[2],
                "CustomOriginConfig": {
                    "HTTPPort": 80,
                    "HTTPSPort": 443,
                    "OriginProtocolPolicy": "https-only",
                    "OriginReadTimeout": 30,
                    "OriginKeepaliveTimeout": 5,
                    "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]},
                },
                "OriginPath": "",
                "ConnectionAttempts": 1,
            }
        )
    controls = (
        cloudfront.list_origin_access_controls()
        .get("OriginAccessControlList", {})
        .get("Items", [])
    )
    control = next(
        (item for item in controls if item["Name"] == "switchboard-lambda"), None
    )
    if control is None:
        control = cloudfront.create_origin_access_control(
            OriginAccessControlConfig={
                "Name": "switchboard-lambda",
                "Description": "Protect Switchboard Lambda URLs",
                "SigningProtocol": "sigv4",
                "SigningBehavior": "always",
                "OriginAccessControlOriginType": "lambda",
            }
        )["OriginAccessControl"]
    for origin in origins:
        origin["OriginAccessControlId"] = control["Id"]
    caches = {
        item["CachePolicy"]["CachePolicyConfig"]["Name"]: item["CachePolicy"]["Id"]
        for item in cloudfront.list_cache_policies(Type="managed")["CachePolicyList"][
            "Items"
        ]
    }
    forwards = {
        item["OriginRequestPolicy"]["OriginRequestPolicyConfig"]["Name"]: item[
            "OriginRequestPolicy"
        ]["Id"]
        for item in cloudfront.list_origin_request_policies(Type="managed")[
            "OriginRequestPolicyList"
        ]["Items"]
    }

    def behavior(origin, cache, mutation=False):
        methods = (
            ["GET", "HEAD", "OPTIONS", "PUT", "PATCH", "POST", "DELETE"]
            if mutation
            else ["GET", "HEAD", "OPTIONS"]
        )
        return {
            "TargetOriginId": origin,
            "ViewerProtocolPolicy": "redirect-to-https",
            "AllowedMethods": {
                "Quantity": len(methods),
                "Items": methods,
                "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]},
            },
            "Compress": True,
            "CachePolicyId": caches[cache],
            "OriginRequestPolicyId": forwards["Managed-AllViewerExceptHostHeader"],
        }

    LOCAL.mkdir(parents=True, exist_ok=True)
    path = LOCAL / "delivery.json"
    current = json.loads(path.read_text()) if path.exists() else None
    if not current:
        distributions = (
            cloudfront.list_distributions().get("DistributionList", {}).get("Items", [])
        )
        existing = next(
            (item for item in distributions if item["Comment"] == "Switchboard AWS"),
            None,
        )
        if existing:
            current = {
                "id": existing["Id"],
                "arn": existing["ARN"],
                "url": "https://" + existing["DomainName"],
                "web_acl": existing["WebACLId"],
            }
    if not current:
        # Create disabled delivery first. No public request reaches an unverified plan.
        acls = waf.list_web_acls(Scope="CLOUDFRONT")["WebACLs"]
        acl = next((item for item in acls if item["Name"] == "switchboard"), None)
        if acl is None:
            acl = waf.create_web_acl(
                Name="switchboard",
                Scope="CLOUDFRONT",
                DefaultAction={"Allow": {}},
                Description="Included in Switchboard CloudFront Free subscription",
                Rules=[
                    {
                        "Name": "LimitRequests",
                        "Priority": 0,
                        "Statement": {
                            "RateBasedStatement": {
                                "Limit": 1000,
                                "AggregateKeyType": "IP",
                            }
                        },
                        "Action": {"Block": {}},
                        "VisibilityConfig": {
                            "SampledRequestsEnabled": False,
                            "CloudWatchMetricsEnabled": False,
                            "MetricName": "switchboard-api-limit",
                        },
                    }
                ],
                VisibilityConfig={
                    "SampledRequestsEnabled": False,
                    "CloudWatchMetricsEnabled": False,
                    "MetricName": "switchboard",
                },
            )["Summary"]
        config = {
            "CallerReference": "switchboard",
            "Aliases": {"Quantity": 0},
            "Origins": {"Quantity": 2, "Items": origins},
            "DefaultCacheBehavior": behavior("website", "Managed-CachingDisabled"),
            "CacheBehaviors": {
                "Quantity": 2,
                "Items": [
                    {
                        "PathPattern": "/api/*",
                        **behavior("api", "Managed-CachingDisabled", True),
                    },
                    {
                        "PathPattern": "/_next/static/*",
                        **behavior("website", "Managed-CachingOptimized"),
                    },
                ],
            },
            "Comment": "Switchboard AWS",
            "Enabled": False,
            "ViewerCertificate": {"CloudFrontDefaultCertificate": True},
            "WebACLId": acl["ARN"],
            "HttpVersion": "http2",
            "IsIPV6Enabled": True,
        }
        distribution = cloudfront.create_distribution(DistributionConfig=config)[
            "Distribution"
        ]
        current = {
            "id": distribution["Id"],
            "arn": distribution["ARN"],
            "url": "https://" + distribution["DomainName"],
            "web_acl": acl["ARN"],
        }
        path.write_text(json.dumps(current, indent=2) + "\n")
    subscriptions = pricing.list_subscriptions()["subscriptionSummaries"]
    subscription = next(
        (
            item
            for item in subscriptions
            if current["arn"] in item.get("resourceArns", [])
        ),
        None,
    )
    if subscription is None:
        subscription = pricing.create_subscription(
            planFamily="CloudFront",
            planTier="FREE",
            resourceArns=[current["arn"], current["web_acl"]],
            clientToken="switchboard-free-" + current["id"],
        )
    subscription = subscription.get("subscription", subscription)
    if subscription.get("planTier") != "FREE" or subscription.get("status") != "ACTIVE":
        raise RuntimeError(
            f"Free subscription is not active: {subscription.get('status')}"
        )
    current["subscription"] = subscription
    path.write_text(json.dumps(current, indent=2, default=str) + "\n")
    print("CloudFront Free subscription created/verified.", flush=True)
    for name in ["api", "website"]:
        for action, suffix, extra in [
            ("lambda:InvokeFunctionUrl", "url", {"FunctionUrlAuthType": "AWS_IAM"}),
            ("lambda:InvokeFunction", "invoke", {"InvokedViaFunctionUrl": True}),
        ]:
            try:
                lambdas.add_permission(
                    FunctionName=f"switchboard-{name}",
                    Qualifier="live",
                    StatementId=f"cloudfront-{suffix}",
                    Action=action,
                    Principal="cloudfront.amazonaws.com",
                    SourceArn=current["arn"],
                    **extra,
                )
            except lambdas.exceptions.ResourceConflictException:
                pass
    response = cloudfront.get_distribution_config(Id=current["id"])
    config = response["DistributionConfig"]
    previous_origins = {item["Id"]: item for item in config["Origins"]["Items"]}
    for origin in origins:
        existing = previous_origins[origin["Id"]]
        existing.update(origin)
    config["Origins"]["Items"] = list(previous_origins.values())
    config["DefaultCacheBehavior"].update(
        behavior("website", "Managed-CachingDisabled")
    )
    for item in config["CacheBehaviors"]["Items"]:
        if item["PathPattern"] == "/api/*":
            item.update(behavior("api", "Managed-CachingDisabled", True))
        elif item["PathPattern"] == "/_next/static/*":
            item.update(behavior("website", "Managed-CachingOptimized"))
    config["Enabled"] = True
    cloudfront.update_distribution(
        Id=current["id"], IfMatch=response["ETag"], DistributionConfig=config
    )
    print(current["url"], flush=True)


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
    arguments = parser.parse_args()
    session = boto3.Session(profile_name=arguments.profile, region_name="us-east-1")
    if arguments.action == "build":
        build(arguments.component, session)
    elif arguments.action == "provision":
        provision(session, arguments.alert_email)
    elif arguments.action == "release":
        release(session, arguments.component, model_provider=arguments.model_provider)
    elif arguments.action == "jobs":
        connect_jobs(session)
    elif arguments.action == "delivery":
        delivery(session)
    else:
        previous = json.loads((LOCAL / "previous-aliases.json").read_text())
        names = (
            {"api", "worker"}
            if arguments.component == "backend"
            else {"website"}
            if arguments.component == "website"
            else set(previous)
        )
        for name, version in previous.items():
            if name not in names:
                continue
            session.client("lambda").update_alias(
                FunctionName=f"switchboard-{name}", Name="live", FunctionVersion=version
            )
            print(f"Restored {name} version {version}.")


if __name__ == "__main__":
    main()

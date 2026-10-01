# AWS operations

[Live application](https://d3ar9mvnjcwzyk.cloudfront.net) · [Architecture](../docs/architecture.md)

The deployment uses four Lambda functions: a Next.js website through AWS Lambda Web Adapter, a FastAPI API through Mangum, a native Python investigation worker, and a signed webhook receiver. Step Functions coordinates the investigation and subsequent manual actions. Parameter Store holds runtime settings and the encrypted model credential. DynamoDB stores application data; SQS delivers jobs; EventBridge Scheduler runs maintenance. CloudFront protects and routes the website and API origins. A small notification Lambda formats operational emails through SNS.

## Deploy

Requirements: Python 3.12 for production packages, uv, Node.js 24, and an authenticated AWS CLI profile in `us-east-1`. Install dependencies in `backend/` and `frontend/`. The Bedrock adapter uses the worker execution role and supports `deepseek.v3.2` and `openai.gpt-6-luna` in `us-east-1`. A release preserves the currently selected provider and Bedrock model unless explicitly overridden. Initial provisioning copies the direct-provider key from the environment or ignored `backend/.env` into `/switchboard/live/deepseek-api-key` as a Standard SecureString using the AWS-managed SSM key. Existing parameters are preserved. Active DeepSeek worker versions reference the parameter rather than storing its value in their environment; Bedrock versions omit that reference.

From the repository root:

```sh
uv run --project backend python scripts/aws.py provision --profile hc-studio
uv run --project backend python scripts/aws.py build --component all --profile hc-studio
uv run --project backend python scripts/aws.py release --component all --profile hc-studio
uv run --project backend python scripts/aws.py jobs --profile hc-studio
uv run --project backend python scripts/aws.py delivery --profile hc-studio
```

`template.json` owns hosting resources, Function URLs, CloudFront/OAC/WAF and its subscription, storage, queues and worker mapping, maintenance schedule, Step Functions, the shared settings parameter, monitoring, and IAM roles. SecureString credentials are supplied separately because CloudFormation does not support that parameter type. The release helper applies Lambda handlers, environment variables, layers, and tracing settings, then publishes code versions. It updates API, worker, and website live aliases directly; the receiver alias is promoted through the `ReceiverVersion` stack parameter. The `jobs` and `delivery` commands verify stack-owned resources. Use `provision` for infrastructure changes and `release` for application code.

Backend builds use Linux Python 3.12 wheels. Website builds include Next.js standalone output, public files, static assets, and one previous release's assets. ZIPs upload directly to Lambda. Package and response limits are checked before deployment.

## Independent releases and rollback

```sh
uv run --project backend python scripts/aws.py build --component website --profile hc-studio
uv run --project backend python scripts/aws.py release --component website --profile hc-studio
uv run --project backend python scripts/aws.py rollback --component website --profile hc-studio
```

Use `--component backend` for API, worker, and receiver, or `--component all` for all functions. Published versions are immutable; `live` aliases select the active release. Keep API/frontend contracts compatible during independent releases. In-flight invocations can finish on their previous version.

The helper saves previous aliases before changing each component to ignored `aws/local/previous-aliases.json`, and package hashes/sizes to `aws/local/packaging.json`. Keep the desired receipt before another release replaces it. Forward releases retain one older static asset set; rollback may require a page reload because an older ZIP cannot contain a future release's assets.

## GitHub Actions

The workflow runs backend and frontend checks, then assumes a repository-scoped role through GitHub OIDC. Set `AWS_ROLE_ARN` to the deployment role. Its trust policy matches GitHub's immutable owner/repository IDs and this repository's `main` branch. Layer access is limited to the pinned Web Adapter and OpenTelemetry layer versions. Main pushes release all functions; manual runs can select a component. Release receipts are saved as Actions artifacts, including after partial failure.

Runtime roles are scoped to application resources. Function URLs require AWS IAM signing and CloudFront Origin Access Control. Only the worker can invoke the configured model in the default Bedrock Mantle project and standard service tier. Only the worker role can read and decrypt the direct-provider credential in Parameter Store; they are not bundled into the website or copied into GitHub secrets.

## Inspect and recover jobs

The worker processes one SQS message per invocation, with maximum concurrency two. Its timeout is 300 seconds; queue visibility is 1,800 seconds. Source retention is four days, dead-letter retention is 14 days, and five receives move a message to the DLQ. A hard failure can therefore have a substantial delay before retry.

Maintenance runs hourly. It recovers pending workflow starts and callbacks lost after committed actions, saves DLQ failures before removing messages, and cleans expired or retired data. Trigger the same internal handler when needed:

```sh
aws lambda invoke --profile hc-studio --function-name switchboard-api:live \
  --cli-binary-format raw-in-base64-out \
  --payload '{"source":"switchboard.maintenance"}' aws/local/maintenance.json
```

Check the response and CloudWatch logs; an invocation acknowledgement alone does not establish success. Logs include run IDs, stages, and attempts, with seven-day retention. Public HTTP requests cannot invoke maintenance or register workflow callbacks.

Investigate repeated failures before requesting another run. Terminal jobs are not automatically replayed. Operator replay requires current access and generation checks; do not blindly redrive stale messages or clear a live worker's lease. Saved validated output avoids repeated model work after some publication failures; work before the checkpoint may repeat.

## Verify storage

```sh
uv run --project backend python scripts/verify_aws.py --profile hc-studio
```

This creates an isolated verification workspace and checks duplicate proposals, atomic receipts, chunked results, and generation fences against DynamoDB, then removes that test workspace. The AWS check also confirms an actual signed receiver delivery. With local development running, the local variant checks storage and generation fences without calling the AWS receiver:

```sh
AWS_ACCESS_KEY_ID=local AWS_SECRET_ACCESS_KEY=local AWS_DEFAULT_REGION=us-east-1 \
  uv run --project backend python scripts/verify_aws.py \
  --table switchboard-local --endpoint-url http://127.0.0.1:8001
```

Local development uses Amazon's official [DynamoDB Local](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/DynamoDBLocal.DownloadingAndRunning.html) image. Its records persist in a Docker volume. Production persistence and asynchronous job tests also run deterministically through the backend suite.

## Monitoring and alerts

Open the `switchboard` CloudWatch dashboard in `us-east-1`. Nine standard alarms publish to the internal `switchboard-alarm-events` topic. A Lambda formats a clear subject, plain-language explanation, UTC timestamp, and investigation links, then publishes to `switchboard-alerts` for the existing email subscription. Raw metric payloads remain in CloudWatch:

| Alarm | Trigger | First check |
| --- | --- | --- |
| APIErrors / WebsiteErrors / ReceiverErrors | An invocation error within five minutes | Function logs and the corresponding trace |
| WorkerErrors | An invocation error within five minutes | Worker logs, model access, and transient service errors |
| QueueBacklog | Oldest queued message exceeds 15 minutes for two five-minute periods | Worker concurrency, retries, and queue visibility |
| DeadLetterJobs | A visible dead-letter message | Saved job state, worker logs, and maintenance results |
| RejectedJobs | A permanently rejected investigation | Worker logs and current access/output validation |
| WorkflowFailures / WorkflowTimeouts | A failed or timed-out execution | Step Functions execution history and saved action receipts |

Connect an email recipient with `provision --alert-email you@example.com`. Confirm the SNS subscription from that inbox; until confirmation, the alarms and topic work but email delivery remains pending. Later provisions retain the recipient when the option is omitted. Pass an empty string to remove the email subscription.

Policy-blocked reports complete normally and do not trigger the rejected-job metric. Investigate failures before replaying jobs; retries can repeat model work before the validated checkpoint.

Policy-review failures include `error_reason` and `report_validation` in the worker's `Investigation rejected` log. These fields retain the rejected report, reviewer or revision output, validation errors, and response finish reason/token counts where available. Recoverable findings appear in `Investigation report requires revision` with the original report and policy issues. Correlate that entry with the job's run ID using the Lambda request ID. Diagnostic content stays in private CloudWatch logs; public job responses retain the safe error message.

## Tracing

All Lambda versions enable active tracing. API, worker, and receiver packages use a pinned OpenTelemetry layer with AWS SDK and Lambda instrumentation; the website retains its Web Adapter and native invocation tracing. The packaging receipt includes the tracing layer's uncompressed size in the Lambda limit check. Tracing permissions grant only X-Ray segment/telemetry publishing; deployment can read only the two pinned layers.

In CloudWatch Traces, filter by `switchboard-api`, `switchboard-worker`, or `switchboard-receiver` and inspect database, queue, and Bedrock timings. Sampling means not every request has a trace. Generic HTTP instrumentation and telemetry logs/metrics exporters are disabled. Model and policy stages record timings. Bedrock request spans also record the model ID and HTTP status; application records, prompts, credentials, and headers are not supplied as trace attributes. Its SDK uses a placeholder key that the signing adapter replaces with refreshed IAM credentials for each request. Requests to another host or path are rejected before signing.

## Check Bedrock access

Before selecting Bedrock for the live worker, verify account access with a small real signed chat-completion request and run the live investigation scenarios. The account must be authorized for the model and have usable invocation quotas. Deterministic tests verify tool schemas, policy-review requests, and error propagation, but do not establish live model availability or quality.

Select a provider explicitly with `release --component backend --model-provider bedrock --bedrock-model openai.gpt-6-luna` or `--model-provider deepseek`. The application does not silently switch providers on a failed call. GitHub releases retain the selected provider.

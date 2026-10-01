# AWS operations

[Live application](https://d3ar9mvnjcwzyk.cloudfront.net) · [Architecture](../docs/architecture.md)

The deployment uses four Lambda functions: a Next.js website through AWS Lambda Web Adapter, a FastAPI API through Mangum, a native Python investigation worker, and a signed webhook receiver. Step Functions coordinates the investigation and subsequent manual actions. Parameter Store holds runtime settings and the encrypted model credential. DynamoDB stores application data; SQS delivers jobs; EventBridge Scheduler runs maintenance. CloudFront protects and routes the website and API origins. A small notification Lambda formats operational emails through SNS.

## Deploy

Use a non-root administrator with MFA for routine operations. `admin-access.json` defines the separate `hc-studio-access` account-access stack and its `hc-studio-admin` user. Administrative actions are denied without MFA; password changes and MFA enrollment remain available. Console passwords are supplied separately, and no permanent access keys are needed. Authenticate the `hc-studio` profile with `aws login --profile hc-studio`; select the administrator session. The application and GitHub deployment use their own IAM roles.

Requirements: Python 3.12, uv, Node.js 24, AWS CLI, and SAM CLI 1.166.2 in `us-east-1`. Install SAM with `uv tool install aws-sam-cli==1.166.2 --python 3.12`, and install the application dependencies in `backend/` and `frontend/`.

From `backend/`:

```sh
uv run python ../scripts/aws.py build --profile hc-studio
uv run python ../scripts/aws.py release --profile hc-studio
uv run python ../scripts/aws.py jobs --profile hc-studio
uv run python ../scripts/aws.py delivery --profile hc-studio
```

SAM builds the Python functions directly from `uv.lock` using its native uv builder. Builds enforce the existing lock and remove development files and environment files before upload. A small Makefile packages the Next.js standalone server, public files, and static assets, retaining one previous release's assets for browsers with older HTML. Lambda Web Adapter and Next.js use port 8081.

`template.yml` combines SAM function definitions with ordinary CloudFormation resources for hosting, protected origins, CloudFront/OAC/WAF, storage, queues, Step Functions, schedules, configuration, monitoring, and scoped IAM roles. SAM uploads artifacts to the private, encrypted, stack-owned `DeploymentArtifacts` bucket. The bucket is for deployment packages; CloudFront serves the website through Lambda.

SAM publishes immutable function versions and selects them through each function's `live` alias. Every release builds the full application. Unchanged Python packages reuse their uploaded object. `AWS::LanguageExtensions` resolves parameter values before SAM generates versions, so a change to the model configuration also publishes a worker version. Model selection, alert recipients, and other existing stack parameters are preserved unless explicitly overridden. Always deploy the original SAM template with its parameter values, rather than reusing a previously processed template.

The public demo uses MiniMax M2.5 through Amazon Bedrock with a 20,000-token completion budget. To select another supported model:

```sh
uv run python ../scripts/aws.py release --profile hc-studio \
  --model-provider bedrock --bedrock-model minimax.minimax-m2.5
```

Direct-provider credentials are Standard SecureString parameters encrypted with the AWS-managed SSM key. They are supplied separately because CloudFormation does not support that parameter type. `model-key` provisions `/switchboard/live/deepseek-api-key` from the environment or ignored `backend/.env` when absent. Only the worker can read and decrypt it; Bedrock worker versions omit the credential reference.

For an initial stack in another account, authenticate an administrator and run the native SAM commands from the repository root. `--resolve-s3` creates SAM's deployment bucket; subsequent releases use the application stack's artifact bucket.

```sh
UV_LOCKED=1 sam build
(cd backend && uv run python ../scripts/sam_artifacts.py clean)
sam deploy --resolve-s3 --profile hc-studio
```

## Deployment checks and rollback

Each release checks the live website, a JavaScript bundle, demo identity, and database-backed persona data. Checks retry transient failures. A failed deployment or health check redeploys the previous template and all its saved runtime parameters through SAM, then checks the restored application. The release remains failed even when recovery succeeds.

```sh
uv run python ../scripts/aws.py check --profile hc-studio
uv run python ../scripts/aws.py rollback --profile hc-studio
```

The helper saves the previous deployable template to ignored `aws/local/previous-template.yml`, its parameters to `previous-deployment.json`, package sizes to `packaging.json`, and the deployment outcome to `deployment-health.json`. Keep those receipts before another release replaces them. A rollback may require a browser reload because an older website package cannot contain a future release's assets.

## GitHub Actions

Main pushes and manual workflow runs validate the backend, frontend, and infrastructure, then build and deploy the full application with the pinned SAM CLI. The workflow assumes a repository-scoped role through GitHub OIDC; `AWS_ROLE_ARN` selects the role. Its trust policy matches GitHub's immutable owner/repository IDs and this repository's `main` branch. Release receipts are saved as Actions artifacts, including after failure.

The deployment role has scoped access to Switchboard's stack and change sets, deployment artifacts, existing functions, and their execution roles. Infrastructure validation supplies the official CloudFormation registry schema for the CloudFront subscription resource. Runtime roles remain scoped to application resources. Function URLs require AWS IAM signing and CloudFront Origin Access Control. Only the worker can invoke the configured Bedrock model in the default Mantle project and standard service tier.

## Inspect and recover jobs

The public demo accepts up to 100 new investigations per UTC day globally and 10 per visitor workspace, controlled by the `DailyJobLimit` and `VisitorDailyJobLimit` stack parameters. Atomic counters commit with new job records. Idempotent resubmissions and SQS retries do not consume another allowance. Resetting a workspace does not reset its daily allowance. Exhaustion returns HTTP 429 without creating or queueing a job. Counter records are removed after three days by the existing bounded hourly maintenance task; business records keep their existing cleanup rules. The application also limits new investigations to 20 globally per five-minute UTC window through `SubmissionWindowLimit`. Existing WAF protection applies to overall traffic.

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

The regional Lambda quota must leave at least 100 executions unreserved. Once the quota supports it, the next application release preserves stack-owned reservations: API 8, website 6, worker 5, receiver 2, and notifications 1. The SQS worker still processes only two messages concurrently. For a new account with a lower quota, set `ReserveConcurrency=disabled` until a Service Quotas increase is approved. Reserved concurrency allocates and caps capacity; it does not keep instances running.

Open the `switchboard` CloudWatch dashboard in `us-east-1`. Ten standard alarms publish to the internal `switchboard-alarm-events` topic. A Lambda formats a clear subject, plain-language explanation, UTC timestamp, and investigation links, then publishes to `switchboard-alerts` for the existing email subscription. Raw metric payloads remain in CloudWatch:

| Alarm | Trigger | First check |
| --- | --- | --- |
| Throttles | Any regional Lambda throttling within five minutes | Regional quota, dashboard concurrency, and function reservations |
| APIErrors / WebsiteErrors / ReceiverErrors | An invocation error within five minutes | Function logs and the corresponding trace |
| WorkerErrors | An invocation error within five minutes | Worker logs, model access, and transient service errors |
| QueueBacklog | Oldest queued message exceeds 15 minutes for two five-minute periods | Worker concurrency, retries, and queue visibility |
| DeadLetterJobs | A visible dead-letter message | Saved job state, worker logs, and maintenance results |
| RejectedJobs | A permanently rejected investigation | Worker logs and current access/output validation |
| WorkflowFailures / WorkflowTimeouts | A failed or timed-out execution | Step Functions execution history and saved action receipts |

Connect an email recipient with `release --alert-email you@example.com`. Confirm the SNS subscription from that inbox; until confirmation, the alarms and topic work but email delivery remains pending. Later releases retain the recipient when the option is omitted. Pass an empty string to remove the email subscription.

Policy-blocked reports complete normally and do not trigger the rejected-job metric. Investigate failures before replaying jobs; retries can repeat model work before the validated checkpoint.

Policy-review failures include `error_reason` and `report_validation` in the worker's `Investigation rejected` log. Empty model responses retain the same diagnostics in `Investigation awaits SQS retry` and leave the job eligible for queue redelivery; they never pass validation or publish a proposal. These fields retain the rejected report, reviewer or revision output, validation errors, and response finish reason/token counts where available. Recoverable findings appear in `Investigation report requires revision` with the original report and policy issues. Correlate that entry with the job's run ID using the Lambda request ID. Diagnostic content stays in private CloudWatch logs; public job responses retain the safe error message.

## Tracing

All Lambda versions enable active tracing. API, worker, and receiver packages use a pinned OpenTelemetry layer with AWS SDK and Lambda instrumentation; the website retains its Web Adapter and native invocation tracing. The packaging receipt includes the tracing layer's uncompressed size in the Lambda limit check. Tracing permissions grant only X-Ray segment/telemetry publishing; deployment can read only the two pinned layers.

In CloudWatch Traces, filter by `switchboard-api`, `switchboard-worker`, or `switchboard-receiver` and inspect database, queue, and Bedrock timings. Sampling means not every request has a trace. Generic HTTP instrumentation and telemetry logs/metrics exporters are disabled. Model and policy stages record timings. Bedrock request spans also record the model ID and HTTP status; application records, prompts, credentials, and headers are not supplied as trace attributes. Its SDK uses a placeholder key that the signing adapter replaces with refreshed IAM credentials for each request. Requests to another host or path are rejected before signing.

## Check Bedrock access

Before selecting Bedrock for the live worker, verify account access with a small real signed chat-completion request and run the live investigation scenarios. The account must be authorized for the model and have usable invocation quotas. Deterministic tests verify tool schemas, policy-review requests, and error propagation, but do not establish live model availability or quality.

Select a provider explicitly with `release --model-provider bedrock --bedrock-model minimax.minimax-m2.5` or `--model-provider deepseek`. The application does not silently switch providers on a failed call. GitHub releases retain the selected provider.

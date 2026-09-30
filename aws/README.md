# AWS operations

[Live application](https://d3ar9mvnjcwzyk.cloudfront.net) · [Architecture](../docs/architecture.md)

The deployment uses three Lambda functions: a Next.js website through AWS Lambda Web Adapter, a FastAPI API through Mangum, and a native Python investigation worker. DynamoDB stores application data; SQS delivers jobs; EventBridge Scheduler runs maintenance. CloudFront protects and routes the website and API origins.

## Deploy

Requirements: Python 3.12 for production packages, uv, Node.js 24, and an authenticated AWS CLI profile in `us-east-1`. Install dependencies in `backend/` and `frontend/`. Initial worker setup reads `DEEPSEEK_API_KEY` from the environment or ignored `backend/.env`; later releases retain the configured worker key when no local value is supplied.

From the repository root:

```sh
uv run --project backend python scripts/aws.py provision --profile hc-studio
uv run --project backend python scripts/aws.py build --component all --profile hc-studio
uv run --project backend python scripts/aws.py release --component all --profile hc-studio
uv run --project backend python scripts/aws.py jobs --profile hc-studio
uv run --project backend python scripts/aws.py delivery --profile hc-studio
```

`template.json` owns the database, queues, functions, logs, and IAM roles through CloudFormation. The helper manages code versions, aliases, Function URLs, CloudFront/OAC/WAF, queue mapping, and scheduled invocation. Use `provision` for infrastructure changes and `release` for application code.

Backend builds use Linux Python 3.12 wheels. Website builds include Next.js standalone output, public files, static assets, and one previous release's assets. ZIPs upload directly to Lambda. Package and response limits are checked before deployment.

## Independent releases and rollback

```sh
uv run --project backend python scripts/aws.py build --component website --profile hc-studio
uv run --project backend python scripts/aws.py release --component website --profile hc-studio
uv run --project backend python scripts/aws.py rollback --component website --profile hc-studio
```

Use `--component backend` for API and worker, or `--component all` for all functions. Published versions are immutable; `live` aliases select the active release. Keep API/frontend contracts compatible during independent releases. In-flight invocations can finish on their previous version.

The helper saves previous aliases before changing each component to ignored `aws/local/previous-aliases.json`, and package hashes/sizes to `aws/local/packaging.json`. Keep the desired receipt before another release replaces it. Forward releases retain one older static asset set; rollback may require a page reload because an older ZIP cannot contain a future release's assets.

## GitHub Actions

The workflow runs backend and frontend checks, then assumes a repository-scoped role through GitHub OIDC. Set `AWS_ROLE_ARN` to the deployment role. Its trust policy restricts access to this repository's `main` branch. Main pushes release all functions; manual runs can select a component. Release receipts are saved as Actions artifacts, including after partial failure.

Runtime roles are scoped to application resources. Function URLs require AWS IAM signing and CloudFront Origin Access Control. Model credentials belong only in the worker's private environment; they are not bundled into the website or copied into GitHub secrets.

## Inspect and recover jobs

The worker processes one SQS message per invocation, with maximum concurrency two. Its timeout is 300 seconds; queue visibility is 1,800 seconds. Source retention is four days, dead-letter retention is 14 days, and five receives move a message to the DLQ. A hard failure can therefore have a substantial delay before retry.

Maintenance runs hourly. It recovers pending dispatch, saves DLQ failures before removing messages, and cleans expired or retired data. Trigger the same internal handler when needed:

```sh
aws lambda invoke --profile hc-studio --function-name switchboard-api:live \
  --cli-binary-format raw-in-base64-out \
  --payload '{"source":"switchboard.maintenance"}' aws/local/maintenance.json
```

Check the response and CloudWatch logs; an invocation acknowledgement alone does not establish success. Logs include run IDs, stages, and attempts, with seven-day retention. Public HTTP requests cannot invoke maintenance.

Investigate repeated failures before requesting another run. Terminal jobs are not automatically replayed. Operator replay requires current access and generation checks; do not blindly redrive stale messages or clear a live worker's lease. Saved validated output avoids repeated model work after some publication failures; work before the checkpoint may repeat.

## Verify storage

```sh
uv run --project backend python scripts/verify_aws.py --profile hc-studio
```

This creates an isolated verification workspace and checks duplicate proposals, atomic receipts, chunked results, and generation fences against DynamoDB, then removes that test workspace. With local development running, use DynamoDB Local instead:

```sh
AWS_ACCESS_KEY_ID=local AWS_SECRET_ACCESS_KEY=local AWS_DEFAULT_REGION=us-east-1 \
  uv run --project backend python scripts/verify_aws.py \
  --table switchboard-local --endpoint-url http://127.0.0.1:8001
```

Local development uses Amazon's official [DynamoDB Local](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/DynamoDBLocal.DownloadingAndRunning.html) image. Its records persist in a Docker volume. Production persistence and asynchronous job tests also run deterministically through the backend suite.

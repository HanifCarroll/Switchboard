# Switchboard

**An AI-assisted workflow for investigating and safely handling customer integration changes.**

[Try the live demo](https://d3ar9mvnjcwzyk.cloudfront.net) · [Architecture](docs/architecture.md) · [Business scenario](docs/endpoint-change-scenario.md)

Switchboard models an internal team managing webhook endpoint changes for a B2B SaaS company. An employee needs to establish who requested a change, whether the destination is registered, which policy applies, and who can approve it. The agent gathers evidence and prepares a recommendation; application code controls the actual business actions.

## Workflow

1. **Investigate:** the agent reads authorized tickets, customer records, integration configuration, and policies.
2. **Validate:** structured output and a separate policy review check the report. Unsupported changes remain blocked.
3. **Propose:** application code checks current records before saving a configuration change proposal.
4. **Approve:** a different assigned technical lead reviews the proposal.
5. **Execute:** the application rechecks authorization, the production change window, approval, and configuration version before recording the change.
6. **Verify:** a separate delivery check records its outcome. Failed or uncertain delivery requires manual intervention.

The dashboard presents evidence, decision criteria, blockers, approvals, and action receipts. Investigations run in the background and can be reopened after navigation or reload. Each visitor receives an isolated workspace and can switch between fictional employee personas to explore the access rules.

The company, customer data, and delivery events are synthetic. Investigations use a real model through Amazon Bedrock or the DeepSeek API; delivery verification sends a signed HTTP event to a separate AWS receiver and checks its durable receipt. Microsoft Entra authentication is supported for configured workspaces; the public demo uses fictional identities.

## Engineering

- **Python, FastAPI, LangGraph, and Pydantic:** a bounded investigation workflow with read-only tools, trusted identity context, structured reports, and one allowed policy revision.
- **Next.js and React:** request and approval views, captured-versus-current evidence, identity-scoped queries, and background job polling.
- **AWS Lambda and CloudFront:** separate website, API, investigation worker, and webhook receiver; protected origins and appropriate response caching.
- **Step Functions:** a durable change workflow that waits for manual approval, execution, and verification while LangGraph runs the investigation.
- **Systems Manager Parameter Store:** shared runtime settings and an encrypted model credential available only to the worker.
- **AWS SAM and CloudFormation:** hosting, protected origins, subscriptions, queues, schedules, workflows, and scoped runtime roles defined as infrastructure.
- **DynamoDB:** workspace generations, conditional business transactions, canonical proposals, immutable result chunks, and retry-safe receipts.
- **SQS and EventBridge Scheduler:** durable submissions, native retries, worker leases, dead-letter handling, and scheduled recovery.
- **Amazon Bedrock:** IAM-authenticated model calls through the Bedrock Mantle Chat Completions API, using the same access-controlled tools and report validation.
- **CloudWatch and OpenTelemetry:** operational alarms, a dashboard, and sampled traces of API requests, queue delivery, database calls, and model calls.
- **GitHub Actions:** deterministic checks, scoped OIDC deployment, SAM deployments, immutable function versions, and readable operational email alerts.

See [architecture and reliability](docs/architecture.md) for the boundaries and tradeoffs, or [AWS operations](aws/README.md) for deployment and recovery commands.

## Run locally

Requirements: Python 3.11+, uv, Node.js 24, AWS CLI, and Docker.

```sh
(cd backend && uv sync)
(cd frontend && npm ci)
./scripts/dev
```

Open http://localhost:3000. The script starts DynamoDB Local, the Python API, and Next.js. Local records persist in the `switchboard-local-data` Docker volume. Local development covers investigation and change management; signed delivery verification uses the deployed AWS receiver. Investigations use deterministic fixtures by default, through the same authorization and persistence rules.

For real investigations, set `DEEPSEEK_API_KEY` in ignored `backend/.env` and run:

```sh
SWITCHBOARD_MODEL_PROVIDER=deepseek SWITCHBOARD_INVESTIGATION_MODE=live ./scripts/dev
```

The Bedrock adapter supports `minimax.minimax-m2.5`, `deepseek.v3.2`, and `openai.gpt-6-luna`. Use an authenticated AWS profile with access to the selected model:

```sh
BEDROCK_PROFILE=hc-studio SWITCHBOARD_MODEL_PROVIDER=bedrock \
  SWITCHBOARD_BEDROCK_MODEL=minimax.minimax-m2.5 \
  SWITCHBOARD_INVESTIGATION_MODE=live ./scripts/dev
```

The `BEDROCK_PROFILE` is separate from the local database credentials. Model access and evaluation quality must be verified before selecting a provider for a hosted release. The public demo uses MiniMax M2.5 through Amazon Bedrock.

For Microsoft sign-in, copy the browser values from `frontend/entra.example.env` to `frontend/.env.local` and server values from `backend/entra.example.env` to `backend/.env`. Local authentication supports both demo and Microsoft sign-in.

## Checks

```sh
(cd backend && uv run ruff check . && uv run ruff format --check .)
(cd backend && uv run pyright && uv run pytest -v)
(cd frontend && npm test && npm run lint && npm run format:check && npm run build)
git diff --check
```

Backend tests cover authorization, report validation, business transitions, DynamoDB transactions, and queue failure scenarios. Frontend tests cover authentication, workflow states, evidence, and mutation behavior. Ordinary tests do not call a model.

Live model evaluations are separate:

```sh
cd backend
uv run pytest evals/test_investigations.py evals/test_policy_faithfulness.py -v
```

## Repository

| Path | Responsibility |
| --- | --- |
| `backend/switchboard/` | API, CLI, investigation workflow, business rules, and persistence |
| `backend/tests/` | Deterministic domain, API, storage, and job tests |
| `backend/evals/` | Explicitly invoked model evaluations |
| `backend/data/` | Synthetic records, scenarios, and withheld evaluation expectations |
| `frontend/` | Next.js dashboard |
| `aws/` | Infrastructure and operations |
| `scripts/` | Local development, packaging, deployment, and storage verification |

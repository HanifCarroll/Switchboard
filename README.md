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

The company, customer data, and delivery events are synthetic. Investigations use a real DeepSeek model; delivery verification is deterministic and does not contact a real webhook. Microsoft Entra authentication is supported for configured workspaces; the public demo uses fictional identities.

## Engineering

- **Python, FastAPI, LangGraph, and Pydantic:** a bounded investigation workflow with read-only tools, trusted identity context, structured reports, and one allowed policy revision.
- **Next.js and React:** request and approval views, captured-versus-current evidence, identity-scoped queries, and background job polling.
- **AWS Lambda and CloudFront:** separate website, API, and investigation worker; protected origins and appropriate response caching.
- **DynamoDB:** workspace generations, conditional business transactions, canonical proposals, immutable result chunks, and retry-safe receipts.
- **SQS and EventBridge Scheduler:** durable submissions, native retries, worker leases, dead-letter handling, and scheduled recovery.
- **GitHub Actions:** deterministic checks, scoped OIDC deployment, immutable function versions, and independent releases.

See [architecture and reliability](docs/architecture.md) for the boundaries and tradeoffs, or [AWS operations](aws/README.md) for deployment and recovery commands.

## Run locally

Requirements: Python 3.11+, uv, Node.js 24, AWS CLI, and Docker.

```sh
(cd backend && uv sync)
(cd frontend && npm ci)
./scripts/dev
```

Open http://localhost:3000. The script starts DynamoDB Local, the Python API, and Next.js. Local records persist in the `switchboard-local-data` Docker volume. Investigations use deterministic fixtures by default, through the same authorization and persistence rules.

For real investigations, set `DEEPSEEK_API_KEY` in ignored `backend/.env`, then run:

```sh
SWITCHBOARD_INVESTIGATION_MODE=live ./scripts/dev
```

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
uv run pytest evals/test_investigations.py -v
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

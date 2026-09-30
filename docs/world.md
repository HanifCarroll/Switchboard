# Switchboard

Switchboard is a fictional B2B SaaS company that connects customers’ business applications. Its product routes events between CRMs, support platforms, and internal services. Each customer has separate sandbox and production integrations. All people and records in this project are synthetic.

## The internal agent

Switchboard’s support and implementation teams handle customer configuration requests. The details are scattered across tickets, account records, configuration settings, and runbooks. An incorrect change can interrupt delivery or send customer data to the wrong destination.

The agent helps employees investigate a request, prepare a change, obtain approval, execute it, and verify the result. It serves Switchboard employees rather than the customers using Switchboard’s product.

The workflow changes one integration’s outbound webhook endpoint: “Move Acme’s CRM event delivery to the new endpoint in ticket CHG-1042.” Credentials, payload formats, and event subscriptions stay outside this workflow.

## Customers and employees

Acme Services routes CRM deal events to its operations system. Globex Software routes CRM deal events to its customer success system. Both call their integration “CRM sync,” so employees must distinguish the customer and environment.

| Employee | Role | Assigned customer |
|---|---|---|
| Alex Rivera | Implementation engineer | Acme |
| Priya Shah | Technical lead | Acme |
| Ben Okafor | Support specialist | Globex |

Support can read assigned customers’ tickets, integration status, and redacted diagnostic summaries, but cannot change configuration. Implementation engineers can inspect configuration, prepare changes, and execute permitted changes. Technical leads can also approve another employee’s production change for their assigned customer.

## Business systems

| System | What it owns |
|---|---|
| Employee directory | Active employees, roles, customer assignments |
| Support desk | Customer requests and correspondence |
| Customer registry | Authorized contacts, registered destinations, production change windows |
| Configuration service | Current integration settings and change history |
| Policy library | Approved procedures and superseded versions |
| Change register | Proposals, approvals, execution results, and verification evidence |

## Business rules

- Employees can access only their assigned customers’ records. Shared internal runbooks are available across accounts. Credentials are not exposed in the agent conversation.
- A change needs a request from an authorized customer contact and a destination registered to that customer.
- Sandbox changes do not require independent approval. Production changes require a separate authorized technical lead and must occur within the customer’s change window.
- Approval covers the exact proposed change. Changed instructions, configuration, or permissions require another review before proceeding.
- Current approved policy takes precedence over superseded runbooks and informal promises. Unresolved conflicts are escalated.
- Success means the intended configuration is active and a test event reaches the intended destination. A failed or uncertain result stays open for investigation or authorized recovery.
- Every change must identify who requested it, who approved it when required, what happened, and how the outcome was verified.

## Everyday complications

- An old runbook permits self-approval.
- A ticket requests an endpoint that has not been registered.
- Two employees submit conflicting changes for the same integration.

These are normal conditions the workflow must handle, not reasons to bypass policy.

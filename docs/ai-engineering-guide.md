# Switchboard AI engineering guide

## What the project does

Switchboard currently processes specific integration change requests from Customer Success (CS), such as changing the address where an app receives events. The AI checks the request using evidence the employee is allowed to see. The application checks current records, creates a change proposal, requires approval from another employee, applies the change, and checks that events arrive.

The demo uses fictional customers and data. Its delivery check sends a real signed request to an AWS receiver.

The planned expansion would let Switchboard investigate problems as well as change requests:

> “Acme stopped receiving events yesterday. Find out why and recommend what to do.”

The AI would check delivery records, recent settings changes, documentation, and earlier incidents. It would explain the likely cause, show its evidence, and ask for missing information. Sometimes the right answer would be to leave the settings alone. Changing the delivery address would remain the first action Switchboard can carry out.

## Two ways to start

| Flow                       | What CS provides                                           | What Switchboard does                                                                                                                                                                                                             |
| -------------------------- | ---------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Requested change (current) | A specific change: “Change Acme's event delivery address.” | Checks the requested change, prepares a proposal, gets independent approval, applies it, and verifies delivery.                                                                                                                   |
| Reported problem (planned) | A problem: “Acme stopped receiving events.”                | Investigates and recommends a fix. If a supported change is needed, the application creates a new internal proposal linked to the original support ticket. That proposal needs its own review and approval before implementation. |

Both flows would remain available. They share the approval, execution, and verification steps, but the proposed fix comes from a different starting point. Reports must distinguish a CS-requested change from an AI-recommended change.

The original support ticket stays in place; an internal change proposal does not require a new Jira or HubSpot ticket. A diagnosis does not supply customer consent or permission to change settings. Any consent required by the business rules must still be obtained and checked. An investigation can also finish without proposing a change.

## Skills covered

| Skill area                    | What works today                                                                                                                                                  | What to add                                                                               | How to check it                                                                                           |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| Product and user experience   | [Findings](../frontend/components/investigation-findings.tsx), evidence, approval steps, and progress updates                                                     | Clear explanations of causes, uncertainty, and next steps                                 | A reviewer can understand the cause and what to do next                                                   |
| Model behavior and prompts    | Trusted employee identity and time, tools that only read data, structured reports, and [policy review](../backend/switchboard/investigation/report_validation.py) | Compare possible causes, read attachments, and ask useful questions                       | Conclusions match the evidence; missing information leads to a question or a clear limit                  |
| Evaluation and error analysis | Tests of investigation outcomes, policy decisions, and business rules                                                                                             | Test diagnosis, search, source references, questions, and resolution across repeated runs | Saved test cases and results show which errors remain                                                     |
| Finding evidence (retrieval)  | Read records by ID, save [evidence snapshots](../backend/switchboard/investigation/evidence.py), and compare them with current records                            | Search documentation and past incidents; track source versions and access                 | The AI finds the right passages and cites the versions it used                                            |
| Tools and integrations        | [Four tools with limited access](../backend/switchboard/investigation/tools.py) and a signed delivery receiver                                                    | Read delivery and settings history; connect Jira Service Management and HubSpot           | Reads respect permissions; approved updates are confirmed in the external system                          |
| Workflow and recovery         | [LangGraph](../backend/switchboard/investigation/workflow.py), Step Functions, SQS, saved progress, and protection against duplicate actions                      | Pause for an answer, resume safely, and finish without proposing a change                 | Interrupted work resumes without repeating completed actions                                              |
| Security and data access      | [Role and assignment checks](../backend/switchboard/auth.py), separate demo workspaces, and restricted AWS permissions                                            | Apply access rules to search and attachments; protect sensitive data                      | Restricted content stays out of AI inputs and reports; instructions in documents cannot grant permissions |
| Monitoring and operations     | [Logs, alerts, traces, and deployment recovery](../aws/README.md)                                                                                                 | Record each run consistently and compare quality, cost, and speed                         | A reader can trace a success or failure back to its versions, attempts, and results                       |

## Planned additions

### Step 1: distinguish two causes of failed deliveries

**Build.** Create two cases: a wrong delivery address and a temporary failure at the receiving system. Give each a known cause, delivery records, and settings history. The AI should tell them apart even when the customer describes the same symptom. Its report should show the cause, evidence, uncertainty, and next step. Include examples of both specific change requests and reported problems, using the same customers and incident facts.

**Decide first.** Add a successful outcome for investigations that recommend no change. The current [result types](../backend/switchboard/models.py) allow a change candidate or a blocked investigation. Define which tools the AI may use and what each case should produce. For change proposals, record the original ticket, whether CS requested the fix or the AI recommended it, and evidence of any required customer consent. Decide how missing consent affects the workflow. Every source reference must point to evidence the AI actually read, was allowed to use, and used correctly. A valid source ID alone does not prove this.

**Done when.** Repeated tests, including new wording, identify both causes correctly. Specific CS change requests still follow the existing review and delivery-check flow. A wrong-address diagnosis creates a separate internal proposal linked to the problem ticket; it can be applied only after required consent and independent approval. The temporary-failure case recommends a useful next step without changing settings unnecessarily. A reviewer can identify who requested or recommended the fix. An unsupported explanation counts as an error even if the final action is correct. Recovery is reported as confirmed only after it has been checked.

### Step 2: search documentation and past incidents

**Build.** Add a small collection of API documentation, operating instructions, and past incidents. Start with keyword search. If it misses useful evidence, test meaning-based search (semantic search) or a mix of both against the same questions.

**Decide first.** Mark the passages each test question should find. Keep each source's ID, version, date, relevant customer or system, access rules, and page or section. Define what happens to search results and saved evidence when a source changes or is deleted. Check access before passing text to the AI.

**Done when.** Searches find the needed passages and source links point to the versions used. Similar but unrelated incidents and outdated instructions do not lead to the wrong recommendation. Test search quality separately from the AI's explanation. Include new wording and restricted documents in the tests.

### Step 3: read attachments and ask for missing information

**Build.** Start with logs attached to a ticket, then add a case where the AI needs to read a screenshot. When information is missing, ask a specific question. Save the question and evidence so the investigation can continue after an employee with permission replies.

**Decide first.** Set file limits and rules for sensitive fields. Keep original attachments and the locations where facts were found. Define the paused and resumed workflow states. Treat files and replies as information to examine; their contents cannot change the employee's identity or permissions.

**Done when.** The AI extracts facts correctly from new test inputs. It handles unreadable files, unclear fields, tables, and repeated uploads. Missing evidence leads to a useful question. Reloading the page, repeated replies, withdrawn access, changed evidence, and processing interruptions do not break recovery or allow an unsupported conclusion.

### Ticket integrations: Jira Service Management and HubSpot

Add both systems once the investigation workflow is useful. Use the existing investigation and approval steps for both.

**Jira first.** Read a customer request, its comments, and attachments. Draft an internal note with the diagnosis, supporting evidence, uncertainty, and next step. Require employee approval before posting the note. The [Jira Service Management API](https://developer.atlassian.com/cloud/jira/service-desk/rest/api-group-request/) supports requests, attachments, and internal or customer-facing comments.

**HubSpot next.** Read a ticket with its related company, contacts, and notes. Use that customer context in the investigation, then prepare a note or ticket update for approval. The [HubSpot Tickets API](https://developers.hubspot.com/docs/api-reference/legacy/crm/objects/tickets/guide) links tickets to these records and activities.

**Done when.** Both connectors read real records, draft useful updates, and confirm approved writes in the external system. Test missing and restricted records, multiple result pages, rate limits, temporary failures, unexpected responses, and timeouts. Retries must not post the same update twice. The AI cannot choose arbitrary server addresses or change the identity used by the connector.

## How to test the additions

Keep the existing access checks, approval steps, evidence records, and recovery controls. If processing stops before a model result is saved, a retry may call the model again. Protection against duplicate external updates is also required.

For each addition:

- Test both starting points. Check that AI-recommended changes stay linked to the problem ticket and cannot be treated as a CS request, customer consent, or employee approval.
- Prepare cases with known causes, required evidence, allowed actions, and situations where the AI should ask a question or say it cannot reach a conclusion. Keep expected answers out of AI inputs. Reserve some cases that are never used to tune the prompts.
- Check the diagnosis, search results, source references, recommended action, questions, and verified recovery. Ask reviewers whether they understand the reports and next steps.
- Use code checks for IDs, permissions, workflow states, and confirmations of completed actions. If another AI grades explanations, compare its judgments with human reviews first. Choose the passing standard before comparing models and record errors for each case.
- Record the code, prompt, model, report format, and source versions, along with attempts, results, time taken, and available usage data. Compare quality, cost, and speed on the same cases, including failed attempts. Report how many cases and repeats were tested, and flag missing usage data.
- Test restricted sources, misleading instructions inside files, and sensitive logs. Define what to hide, how long to keep data, and who can see it. Remove credentials and unnecessary customer information from shared results.

## Where to find the implementation and tests

- [Architecture](architecture.md): how the application and workflow fit together.
- [Investigation evaluations](../backend/evals/test_investigations.py), [policy evaluations](../backend/evals/test_policy_faithfulness.py), and [reference cases](../backend/data/evaluations/): current outcome and policy checks. Most written explanations still need separate quality checks.
- [Backend tests](../backend/tests/): business rules, storage, background jobs, evidence, and access checks.
- [AWS operations](../aws/README.md) and [deployment workflow](../.github/workflows/deploy.yml): deployment checks and recovery.

---
id: endpoint-change-v2
title: Endpoint change policy
version: 2
owner: Implementation Operations
access: shared_internal
status: approved
effective_on: 2026-09-01
supersedes: endpoint-change-v1
---

# Endpoint change policy — version 2

This policy replaces version 1. Engineers may no longer approve their own production changes.

## Access and evidence

Employees may access only their assigned customers' records. Support specialists can investigate tickets and read integration status and redacted diagnostic summaries, but cannot change configuration. Implementation engineers and technical leads can prepare and execute permitted changes. Credentials must not appear in the agent conversation.

Before preparing a proposal, verify the authorized customer contact, registered destination for that customer and environment, and current configuration using the customer registry and configuration records. Ticket claims alone are insufficient. Missing access to these records blocks proposal preparation until an authorized employee retrieves and verifies the evidence. An unregistered destination blocks the change until registration is completed separately.

## Approval and execution

Preparing a proposal creates a draft for review and makes no configuration change. Independent approval and an open change window are not prerequisites for preparing this draft; they apply before execution.

Sandbox changes do not require independent approval. Production changes require a different technical lead assigned to the customer and must occur within the registered change window.

Approval covers the exact customer, integration, environment, current configuration, proposed endpoint, and recovery plan. Recheck permissions and configuration before execution. Changed instructions, configuration, or authority require review again. A conflicting request must be resolved before proceeding.

## Verification and recovery

Confirm that the approved endpoint is active and that the intended destination received a synthetic test event. A configuration update alone is not proof of successful delivery.

If verification fails, restore the old endpoint only when the approved plan permits it and no intervening change makes restoration unsafe. Otherwise, stop for manual intervention. An uncertain result remains open for manual investigation; do not report completion or repeat an action without establishing what occurred.

Record the requester, approver where required, exact change, execution result, and verification evidence in the change register. Update the support ticket with the actual outcome.

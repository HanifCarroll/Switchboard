# Worked scenario: Acme endpoint change

This fictional example illustrates the workflow defined in [world.md](world.md). Addresses are illustrative and use the reserved `.example` domain.

## Request and starting conditions

Alex Rivera asks: “Move Acme’s production CRM sync to the new endpoint in CHG-1042.”

The ticket comes from Jordan Lee, an authorized Acme contact. It requests replacing `https://old.acme.example/deals` with `https://events.acme.example/deals`. Both destinations are registered to Acme. Execution must fall within Acme’s configured change window. The public demo opens that window for the current day so visitors can complete the workflow.

Alex is assigned to Acme. Priya Shah is its technical lead. The configuration service confirms the old endpoint is active. An old runbook allows engineers to approve their own changes, but the current approved policy requires independent approval.

## From investigation to completion

1. **Investigate.** The agent checks Alex’s access and gathers Acme’s ticket, registered destinations, current configuration, and applicable policy. Globex’s identically named integration is outside Alex’s access.
2. **Explain.** The agent confirms that the request is supported and identifies the outdated runbook. It explains that Priya must approve the production change.
3. **Propose.** Alex receives the exact old and new endpoints, supporting records, and a plan to verify delivery with a synthetic event. Failed or uncertain verification requires manual intervention; the application does not automatically restore the old endpoint.
4. **Approve.** Priya reviews and approves this proposal. Until then, production remains unchanged.
5. **Execute.** The application confirms that permissions, configuration, and the change window still permit the approved action, then applies the endpoint change.
6. **Verify.** The application checks the active endpoint and configuration version, then sends a signed synthetic event to the configured AWS receiver. A matching receipt establishes delivery; a failure or timeout stops the flow for manual intervention.
7. **Close.** The change register records the request, approval, applied change, and verification evidence. The support ticket receives the verified outcome.

## Expected outcome

Acme’s production integration points to the approved new endpoint. Its sandbox and every Globex integration remain unchanged. The employee can inspect evidence of approval, execution, and test delivery.

If the request cannot be authorized, the proposal changes, or verification is inconclusive, the agent reports the specific blocker instead of claiming completion.

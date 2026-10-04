export type Approval = {
  id: string;
  approved_by_employee_id: string;
  created_at: string;
};
export type Execution = {
  id: string;
  proposal_id: string;
  executed_by_employee_id: string;
  executed_at: string;
  approval_id: string | null;
  previous_configuration_version: number;
  resulting_configuration_version: number;
};
export type ExecuteProposalResult = { execution: Execution; was_created: boolean };
export type DeliveryVerification = {
  id: string;
  execution_id: string;
  proposal_id: string;
  verified_by_employee_id: string;
  outcome: "delivered" | "failed" | "inconclusive";
  test_event_id: string;
  destination: string;
  evidence: string;
  verified_at: string;
};
export type VerifyDeliveryResult = {
  verification: DeliveryVerification;
  was_created: boolean;
};
export type ProposalReviewResult = {
  execution: Execution | null;
  verification: DeliveryVerification | null;
  current_status: WorkflowStatus;
  proposal: {
    id: string;
    ticket_id: string;
    customer_id: string;
    integration_id: string;
    environment: string;
    current_endpoint: string;
    proposed_endpoint: string;
    expected_configuration_version: number;
    recovery_plan: "manual_intervention";
    proposed_by_employee_id: string;
    created_at: string;
  };
  approval: Approval | null;
};

export type WorkflowStatus = {
  code:
    | "ready_to_investigate"
    | "configuration_updated"
    | "delivery_verified"
    | "manual_intervention_required"
    | "blocked"
    | "awaiting_approval"
    | "approval_recorded"
    | "approval_not_required"
    | "unavailable";
  title: string;
  next_action: string;
};

export type DemoPersona = { id: string; name: string; role: string };
export type PersonReference = { id: string; name: string };
export type Ticket = {
  id: string;
  customer_id: string;
  integration_id: string;
  requester_contact_id: string;
  assigned_employee_id: string;
  requester: PersonReference;
  assigned_employee: PersonReference;
  requested_endpoint: string;
  created_at: string;
  status: string;
  subject: string;
  body: string;
};
export type AssignedRequestSummary = Ticket & {
  workflow_status: WorkflowStatus;
  needs_attention: boolean;
};
export type InvestigationHistoryItem = {
  run_id: string;
  ticket_id: string;
  scenario_id: string | null;
  outcome: string;
};
export type CriterionStatus = "verified" | "unverified" | "unavailable" | "failed" | "deferred";
export type WorkflowStage = "proposal" | "review" | "execution" | "verification";
export type DecisionCriterion = {
  name: string;
  status: CriterionStatus;
  required_before: WorkflowStage;
  explanation: string;
  policy_id: string | null;
  evidence_ids: string[];
};
export type InvestigationBlocker = {
  kind: "missing_evidence" | "confirmed_violation";
  summary: string;
  resolution: string;
};
export type InvestigationRun = {
  status?: "completed";
  current_status: WorkflowStatus;
  run_id: string;
  ticket_id: string;
  scenario_id: string | null;
  result: {
    source: "model" | "fixture";
    investigation: {
      outcome: string;
      findings: {
        overview: string;
        decision_criteria: DecisionCriterion[];
        recommendation: string;
      };
      blockers: InvestigationBlocker[];
      evidence_ids: string[];
    };
    report_validation: {
      policy_ids: string[];
      evaluation_count: number;
      revision_count: number;
    };
    evidence: EvidenceSnapshot[];
    proposal: { id: string } | null;
    was_created: boolean | null;
    messages: {
      tool_calls?: {
        id: string;
        name: string;
        args: Record<string, unknown>;
      }[];
    }[];
  };
};

export type InvestigationJob = {
  run_id: string;
  ticket_id: string;
  status: "queued" | "running" | "completed" | "failed";
  error: string | null;
  retryable: boolean;
};
export type InvestigationResponse = InvestigationRun | InvestigationJob;

function submissionStorageKey(identity: RequestIdentity, ticketId: string) {
  const workspace = sessionStorage.getItem("switchboard-workspace") ?? "local";
  return JSON.stringify(["switchboard-submission", workspace, ...identityKey(identity), ticketId]);
}

export function investigationRequestKey(identity: RequestIdentity, ticketId: string) {
  const key = submissionStorageKey(identity, ticketId);
  const existing = sessionStorage.getItem(key);
  if (existing) return JSON.parse(existing).key as string;
  const requestKey = crypto.randomUUID();
  sessionStorage.setItem(key, JSON.stringify({ key: requestKey, runId: null }));
  return requestKey;
}

export function rememberSubmission(identity: RequestIdentity, response: InvestigationResponse) {
  if (typeof sessionStorage === "undefined") return;
  const key = submissionStorageKey(identity, response.ticket_id);
  const existing = sessionStorage.getItem(key);
  if (!existing) return;
  const submission = JSON.parse(existing);
  if (submission.runId && submission.runId !== response.run_id) return;
  if ("result" in response || response.status === "failed") {
    sessionStorage.removeItem(key);
  } else {
    sessionStorage.setItem(key, JSON.stringify({ ...submission, runId: response.run_id }));
  }
}

export type EvidenceKind = "ticket" | "customer" | "integration" | "policy";
export type EvidenceSnapshot = {
  id: string;
  kind: EvidenceKind;
  captured_at: string;
  document: Record<string, unknown>;
};
export type InvestigationEvidenceDetail = {
  snapshot: EvidenceSnapshot;
  current_document: Record<string, unknown> | null;
  has_changed: boolean | null;
};

export type RequestIdentity = { mode: "demo"; employeeId: string };

class ApiRequestError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

const RETRYABLE_READ_STATUSES = new Set([408, 425, 429, 500, 502, 503, 504]);

/** Retry temporary failures for idempotent queries while a hosted service starts. */
export function shouldRetryReadRequest(failureCount: number, error: Error) {
  if (failureCount >= 6) return false;

  return (
    error instanceof TypeError ||
    (error instanceof ApiRequestError && RETRYABLE_READ_STATUSES.has(error.status))
  );
}

export function readRetryDelay(attemptIndex: number) {
  return Math.min(500 * 2 ** attemptIndex, 5_000);
}

/** Send an explicitly selected identity to the Python API. */
export async function requestApi<T>({
  path,
  identity,
  options = {},
}: {
  path: string;
  identity: RequestIdentity;
  options?: RequestInit;
}): Promise<T> {
  // 1. Keep identity headers under this helper's control.
  const headers = new Headers(options.headers);
  if (
    headers.has("Authorization") ||
    headers.has("X-Demo-Persona-Id") ||
    headers.has("X-Switchboard-Authorization") ||
    headers.has("X-Amz-Content-Sha256")
  ) {
    throw new Error("Do not supply identity headers through request options.");
  }
  headers.set("Content-Type", "application/json");

  // 2. Send only the selected fictional employee to the same-origin demo API.
  if (identity.mode !== "demo") throw new Error("Only demo profiles are supported.");
  if (!path.startsWith("/api/")) throw new Error("Use a same-origin demo API path.");
  headers.set("X-Demo-Persona-Id", identity.employeeId);

  if (process.env.NEXT_PUBLIC_AWS_DEPLOYMENT === "1" && options.body != null) {
    if (typeof options.body !== "string")
      throw new Error("Signed API requests require a JSON body.");
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(options.body));
    headers.set(
      "X-Amz-Content-Sha256",
      Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join(""),
    );
  }

  // 3. Send once and surface failures; never retry a business action here.
  const response = await fetch(path, {
    ...options,
    cache: "no-store",
    headers,
  });
  const workspace = response.headers.get("X-Switchboard-Workspace");
  if (workspace && typeof sessionStorage !== "undefined")
    sessionStorage.setItem("switchboard-workspace", workspace);
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiRequestError(
      typeof body?.detail === "string"
        ? body.detail
        : "The service is temporarily unavailable. Try again.",
      response.status,
    );
  }
  return response.json();
}

export type CurrentEmployee = { employee_id: string; name: string; role: string };

export function identityKey(identity: RequestIdentity) {
  return ["demo", identity.employeeId];
}

// Keep business caches separate for every fictional employee.
export const investigationKeys = {
  tickets: (identity: RequestIdentity) => ["tickets", ...identityKey(identity)] as const,
  history: (identity: RequestIdentity, ticketId: string | null) =>
    ["investigations", ...identityKey(identity), ticketId] as const,
  run: (identity: RequestIdentity, runId: string | null) =>
    ["investigation", ...identityKey(identity), runId] as const,
  evidence: (identity: RequestIdentity, runId: string, evidenceId: string) =>
    ["investigation-evidence", ...identityKey(identity), runId, evidenceId] as const,
};

export const ticketsQuery = (identity: RequestIdentity) => ({
  queryKey: investigationKeys.tickets(identity),
  queryFn: ({ signal }: { signal: AbortSignal }) =>
    requestApi<AssignedRequestSummary[]>({
      path: "/api/tickets",
      identity,
      options: { signal },
    }),
});

export const demoPersonasQuery = (identity: RequestIdentity) => ({
  queryKey: ["demo-personas", ...identityKey(identity)],
  queryFn: ({ signal }: { signal: AbortSignal }) =>
    requestApi<DemoPersona[]>({
      path: "/api/demo/personas",
      identity,
      options: { signal },
    }),
  staleTime: Infinity,
});

export function historyQuery(identity: RequestIdentity, ticketId: string | null) {
  return {
    queryKey: investigationKeys.history(identity, ticketId),
    enabled: ticketId !== null,
    refetchInterval: (query: { state: { data: InvestigationHistoryItem[] | undefined } }) =>
      query.state.data?.some((item) => item.outcome === "queued" || item.outcome === "running")
        ? 2000
        : false,
    queryFn: ({ signal }: { signal: AbortSignal }) => {
      if (!ticketId) throw new Error("Select a ticket first.");
      return requestApi<InvestigationHistoryItem[]>({
        path: `/api/investigations?ticket_id=${encodeURIComponent(ticketId)}`,
        identity,
        options: { signal },
      });
    },
  };
}

export function investigationQuery(identity: RequestIdentity, runId: string | null) {
  return {
    queryKey: investigationKeys.run(identity, runId),
    enabled: runId !== null,
    refetchInterval: (query: { state: { data: InvestigationResponse | undefined } }) =>
      query.state.data &&
      !("result" in query.state.data) &&
      (query.state.data.status === "queued" || query.state.data.status === "running")
        ? 2000
        : false,
    queryFn: async ({ signal }: { signal: AbortSignal }) => {
      if (!runId) throw new Error("Select an investigation first.");
      const response = await requestApi<InvestigationResponse>({
        path: `/api/investigations/${runId}`,
        identity,
        options: { signal },
      });
      rememberSubmission(identity, response);
      return response;
    },
  };
}

export function investigationEvidenceQuery(
  identity: RequestIdentity,
  runId: string,
  evidenceId: string,
) {
  return {
    queryKey: investigationKeys.evidence(identity, runId, evidenceId),
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      requestApi<InvestigationEvidenceDetail>({
        path: `/api/investigations/${encodeURIComponent(runId)}/evidence/${encodeURIComponent(evidenceId)}`,
        identity,
        options: { signal },
      }),
  };
}

export const proposalReviewKeys = {
  inbox: (identity: RequestIdentity) => ["approval-inbox", ...identityKey(identity)] as const,
  proposal: (runId: string, proposalId: string) => ["proposal-review", runId, proposalId] as const,
};

export type ApprovalInboxItem = {
  run_id: string;
  proposal: ProposalReviewResult["proposal"];
};

export const approvalInboxQuery = (identity: RequestIdentity) => ({
  queryKey: proposalReviewKeys.inbox(identity),
  queryFn: ({ signal }: { signal: AbortSignal }) =>
    requestApi<ApprovalInboxItem[]>({
      path: "/api/approvals",
      identity,
      options: { signal },
    }),
});

export function proposalReviewQuery({
  identity,
  runId,
  proposalId,
}: {
  identity: RequestIdentity;
  runId: string;
  proposalId: string;
}) {
  return {
    queryKey: [...proposalReviewKeys.proposal(runId, proposalId), ...identityKey(identity)],
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      requestApi<ProposalReviewResult>({
        path: `/api/runs/${runId}/proposals/${proposalId}`,
        identity,
        options: {
          signal,
        },
      }),
    staleTime: 0,
    gcTime: 0,
  };
}

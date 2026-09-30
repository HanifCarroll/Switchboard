"use client";

import { useState } from "react";
import { useMutation, useIsMutating, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import {
  ArrowLeft,
  Building2,
  CircleAlert,
  Database,
  LoaderCircle,
  Search,
  ShieldCheck,
} from "lucide-react";
import { Alert, AlertTitle, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { ApprovalInbox } from "@/components/approval-inbox";
import { ProposalReview } from "@/components/proposal-review";
import {
  approvalInboxQuery,
  demoPersonasQuery,
  historyQuery,
  investigationEvidenceQuery,
  investigationQuery,
  investigationKeys,
  type InvestigationResponse,
  investigationRequestKey,
  rememberSubmission,
  requestApi,
  ticketsQuery,
  proposalReviewKeys,
} from "@/lib/api";
import { DemoPersonaSwitcher } from "@/components/demo-persona";
import { InvestigationHistory } from "@/components/investigation-history";
import { InvestigationFindings } from "@/components/investigation-findings";
import { EvidenceSheet } from "@/components/evidence-sheet";
import { TicketDetail } from "@/components/ticket-detail";
import { TicketList } from "@/components/ticket-list";
import { WorkflowProgress } from "@/components/workflow-progress";
import { WorkspaceSidebar } from "@/components/workspace-sidebar";
import { useWorkspaceSession } from "@/components/workspace-provider";
import { approvalPath, requestPath, workspacePaths } from "@/lib/workspace-routes";

export type WorkspaceRouteDescriptor =
  | { kind: "work" }
  | { kind: "request"; ticketId: string; runId?: string; evidenceId?: string }
  | { kind: "approvals"; proposalId?: string; runId?: string };

export function WorkspaceRoute({ route }: { route: WorkspaceRouteDescriptor }) {
  const { identity, currentEmployee, accountActions, onEmployeeChange } = useWorkspaceSession();
  const router = useRouter();
  const employee = identity.mode === "demo" ? identity.employeeId : currentEmployee!.employee_id;
  const queryClient = useQueryClient();
  const selectedTicketId = route.kind === "request" ? route.ticketId : null;
  const activeView = route.kind === "approvals" ? "approvals" : "work";
  const [ticketSearch, setTicketSearch] = useState("");
  const [ticketFilter, setTicketFilter] = useState<"all" | "needs_attention">("all");

  // 1. Query keys isolate each employee's data; Query manages cancellation and loading.
  const personasQuery = useQuery({
    ...demoPersonasQuery(identity),
    enabled: identity.mode === "demo",
  });
  const ticketsQueryResult = useQuery(ticketsQuery(identity));
  const approvalsQueryResult = useQuery({
    ...approvalInboxQuery(identity),
    enabled: activeView === "approvals",
  });
  const mutationsInProgress = useIsMutating();
  const historyQueryResult = useQuery(historyQuery(identity, selectedTicketId));
  const selectedRunId =
    (route.kind === "request" ? route.runId : null) ??
    historyQueryResult.data?.find((item) => item.outcome === "queued" || item.outcome === "running")
      ?.run_id ??
    null;
  const selectedRun = useQuery(investigationQuery(identity, selectedRunId));
  const evidenceQueryResult = useQuery({
    ...investigationEvidenceQuery(
      identity,
      route.kind === "request" ? (route.runId ?? "") : "",
      route.kind === "request" ? (route.evidenceId ?? "") : "",
    ),
    enabled: route.kind === "request" && Boolean(route.runId && route.evidenceId),
  });
  const personas = personasQuery.data ?? [];
  const tickets = ticketsQueryResult.data ?? [];
  const approvals = approvalsQueryResult.data ?? [];
  const selectedProposalId = route.kind === "approvals" ? (route.proposalId ?? null) : null;
  const selectedApproval =
    approvals.find((item) => item.proposal.id === selectedProposalId) ?? null;
  const selectedApprovalRunId =
    route.kind === "approvals" ? (route.runId ?? selectedApproval?.run_id ?? null) : null;
  const normalizedTicketSearch = ticketSearch.trim().toLowerCase();
  const visibleTickets = tickets.filter(
    (ticket) =>
      (ticketFilter === "all" || ticket.needs_attention) &&
      (!normalizedTicketSearch ||
        [ticket.id, ticket.subject, ticket.customer_id, ticket.integration_id].some((value) =>
          value.toLowerCase().includes(normalizedTicketSearch),
        )),
  );
  const needsAttentionCount = tickets.filter((ticket) => ticket.needs_attention).length;
  const selectedTicket = tickets.find((ticket) => ticket.id === selectedTicketId) ?? null;
  const history = historyQueryResult.data ?? [];
  const response = selectedRun.isError ? null : selectedRun.data;
  const run = response && "result" in response ? response : null;
  const job = response && !("result" in response) ? response : null;
  const jobActive = job?.status === "queued" || job?.status === "running";
  const employeeRecord = personas.find((item) => item.id === employee);
  const employeeName = employeeRecord?.name ?? currentEmployee?.name ?? employee;
  const employeeRole = employeeRecord?.role ?? currentEmployee?.role ?? null;
  const reconnecting = [
    personasQuery,
    ticketsQueryResult,
    approvalsQueryResult,
    historyQueryResult,
    selectedRun,
    evidenceQueryResult,
  ].some((query) => query.failureCount > 0 && query.fetchStatus === "fetching");

  // 2. Mutations run only on explicit user actions and never retry paid calls.
  const investigation = useMutation({
    mutationFn: (ticketId: string) =>
      requestApi<InvestigationResponse>({
        path: "/api/investigations",
        identity,
        options: {
          method: "POST",
          body: JSON.stringify({
            ticket_id: ticketId,
            idempotency_key: investigationRequestKey(identity, ticketId),
          }),
        },
      }),
    onSuccess: (savedRun) => {
      queryClient.setQueryData(investigationKeys.run(identity, savedRun.run_id), savedRun);
      rememberSubmission(identity, savedRun);
      router.replace(requestPath(savedRun.ticket_id, savedRun.run_id));
    },
    onSettled: (_data, _error, ticketId) =>
      queryClient.invalidateQueries({ queryKey: investigationKeys.history(identity, ticketId) }),
  });
  function startTicketInvestigation(ticketId: string) {
    investigation.mutate(ticketId);
  }

  function selectTicket(ticketId: string) {
    investigation.reset();
    router.push(requestPath(ticketId));
  }

  function changePersona(personaId: string) {
    investigation.reset();
    onEmployeeChange(personaId);
    router.push(workspacePaths.work);
  }

  function clearSelection() {
    router.push(workspacePaths.work);
  }

  function openApprovals() {
    investigation.reset();
    router.push(workspacePaths.approvals);
  }

  function openRun(id: string) {
    investigation.reset();
    if (selectedTicketId) router.replace(requestPath(selectedTicketId, id));
  }

  function refreshHistory() {
    void historyQueryResult.refetch();
  }

  // 3. Derive presentation from query and mutation state, rather than copying it.
  const pendingMessage = investigation.isPending
    ? "Investigating records and validating the result…"
    : jobActive
      ? job.status === "queued"
        ? "Investigation queued. You can leave this page and return later."
        : "Investigating records. Temporary failures retry automatically; recovery can take up to 30 minutes."
      : reconnecting
        ? "Reconnecting to the service…"
        : selectedRun.isLoading
          ? "Loading saved investigation…"
          : "";
  const isPending = pendingMessage !== "";
  const operationError = investigation.error?.message;
  const evidenceError = evidenceQueryResult.error?.message;
  const readError = (
    selectedRun.error ??
    ticketsQueryResult.error ??
    approvalsQueryResult.error ??
    personasQuery.error ??
    historyQueryResult.error
  )?.message;
  const error = operationError ?? readError ?? job?.error;

  function retryWorkspaceReads() {
    void queryClient.refetchQueries({ type: "active" });
  }

  function closeEvidence() {
    if (route.kind !== "request") return;

    router.replace(requestPath(route.ticketId, route.runId), { scroll: false });
  }
  const demoPersonaSwitcher =
    identity.mode === "demo" ? (
      <DemoPersonaSwitcher
        personas={personas}
        selectedPersonaId={employee}
        busy={isPending || mutationsInProgress > 0}
        onChange={changePersona}
      />
    ) : undefined;

  return (
    <div className="min-h-screen bg-[#fcfcfb] lg:pl-60">
      <WorkspaceSidebar
        activeView={activeView}
        employee={employeeName}
        role={employeeRole}
        accountControls={accountActions}
        demoPersona={identity.mode === "demo" ? (employeeRecord ?? null) : undefined}
        demoPersonaSwitcher={demoPersonaSwitcher}
        onOpenWork={clearSelection}
        onOpenApprovals={openApprovals}
      />

      {activeView === "approvals" ? (
        <main className="min-h-screen">
          <div className="px-5 py-7 sm:px-8">
            <div className="mx-auto max-w-6xl">
              <h1 className="text-3xl font-semibold tracking-tight">Approvals</h1>
              <p className="mt-1 text-sm text-muted-foreground">
                Production changes waiting for your independent review.
              </p>
            </div>
          </div>
          <div className="mx-auto max-w-6xl px-5 py-8 sm:px-8">
            {error && (
              <WorkspaceError
                message={error}
                onRetry={operationError ? undefined : retryWorkspaceReads}
              />
            )}
            <section className="overflow-hidden rounded-xl border bg-white shadow-sm">
              <div className="flex items-center justify-between gap-3 px-5 py-4">
                <div>
                  <h2 className="font-semibold">Awaiting review</h2>
                  <p className="mt-1 text-sm text-muted-foreground">
                    Only proposals you are currently authorized to approve appear here.
                  </p>
                </div>
                <Badge variant="secondary">{approvals.length} pending</Badge>
              </div>
              <ApprovalInbox
                items={approvals}
                selectedProposalId={selectedProposalId}
                busy={approvalsQueryResult.isFetching}
                onSelect={(item) =>
                  router.push(
                    approvalPath({
                      proposalId: item.proposal.id,
                      runId: item.run_id,
                    }),
                  )
                }
              />
            </section>

            {selectedProposalId && selectedApprovalRunId && (
              <section className="mt-8 rounded-xl border bg-white px-6 shadow-sm">
                <ProposalReview
                  key={selectedProposalId}
                  runId={selectedApprovalRunId}
                  proposalId={selectedProposalId}
                  identity={identity}
                  currentEmployee={currentEmployee}
                  employees={personas}
                  onStatusRefresh={async () => {
                    await queryClient.invalidateQueries({
                      queryKey: proposalReviewKeys.inbox(identity),
                    });
                  }}
                />
              </section>
            )}
          </div>
        </main>
      ) : !selectedTicket ? (
        <main className="min-h-screen">
          <div className="px-5 py-7 sm:px-8">
            <div className="mx-auto max-w-6xl">
              <div className="flex flex-wrap items-end justify-between gap-5">
                <div>
                  <h1 className="text-3xl font-semibold tracking-tight">My work</h1>
                  <p className="mt-1 text-sm text-muted-foreground">
                    Customer requests assigned to your employee account.
                  </p>
                </div>
                <div className="relative w-full max-w-sm">
                  <Search
                    className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground"
                    aria-hidden="true"
                  />
                  <input
                    type="search"
                    aria-label="Search assigned requests"
                    placeholder="Search requests"
                    value={ticketSearch}
                    onChange={(event) => setTicketSearch(event.target.value)}
                    className="h-10 w-full rounded-lg border bg-white pr-3 pl-9 text-sm outline-none focus:ring-2 focus:ring-ring/30"
                  />
                </div>
              </div>
              <div className="mt-6 flex flex-wrap items-center gap-2 text-sm">
                <button
                  type="button"
                  className={`rounded-md px-3 py-1.5 font-medium ${ticketFilter === "all" ? "bg-blue-50 text-blue-700" : "text-muted-foreground hover:bg-slate-100"}`}
                  onClick={() => setTicketFilter("all")}
                >
                  All {tickets.length}
                </button>
                <button
                  type="button"
                  className={`rounded-md px-3 py-1.5 font-medium ${ticketFilter === "needs_attention" ? "bg-blue-50 text-blue-700" : "text-muted-foreground hover:bg-slate-100"}`}
                  onClick={() => setTicketFilter("needs_attention")}
                >
                  Needs attention {needsAttentionCount}
                </button>
              </div>
            </div>
          </div>
          <div className="mx-auto max-w-6xl px-5 py-8 sm:px-8">
            {error && (
              <WorkspaceError
                message={error}
                onRetry={operationError ? undefined : retryWorkspaceReads}
              />
            )}
            {isPending && <PendingOperation message={pendingMessage} />}
            <section className="overflow-hidden rounded-xl border bg-white shadow-sm">
              <div className="flex items-center justify-between gap-3 px-5 py-4">
                <h2 className="font-semibold">Assigned requests</h2>
                <Badge variant="secondary">{tickets.length} open</Badge>
              </div>
              <TicketList
                tickets={visibleTickets}
                selectedTicketId={selectedTicketId}
                busy={ticketsQueryResult.isFetching}
                onSelect={selectTicket}
              />
            </section>
          </div>
        </main>
      ) : (
        <main className="min-h-screen" aria-live="polite" aria-busy={isPending}>
          <header className="px-5 py-6 sm:px-8">
            <div className="mx-auto max-w-7xl">
              <Button variant="ghost" size="sm" className="-ml-3" onClick={clearSelection}>
                <ArrowLeft className="size-4" />
                My work
              </Button>
              <div className="mt-4 flex flex-wrap items-start justify-between gap-4">
                <div>
                  <p className="text-sm font-medium text-muted-foreground">{selectedTicket.id}</p>
                  <h1 className="mt-1 text-3xl font-semibold tracking-tight">
                    {selectedTicket.subject}
                  </h1>
                  <div className="mt-4 flex flex-wrap items-center gap-x-5 gap-y-2 text-sm text-muted-foreground">
                    <span className="flex items-center gap-2">
                      <Building2 className="size-4" aria-hidden="true" />
                      {selectedTicket.customer_id}
                    </span>
                    <span className="flex items-center gap-2">
                      <Database className="size-4" aria-hidden="true" />
                      {selectedTicket.integration_id}
                    </span>
                    <span>Owner: {selectedTicket.assigned_employee.name}</span>
                    <Badge variant="secondary" className="capitalize">
                      {run?.current_status.title ?? selectedTicket.status}
                    </Badge>
                  </div>
                </div>
              </div>
            </div>
          </header>

          <div className="mx-auto max-w-7xl px-5 sm:px-8">
            <WorkflowProgress
              hasInvestigation={run !== null}
              hasProposal={run?.result.proposal !== null && run?.result.proposal !== undefined}
              investigationInProgress={investigation.isPending || jobActive}
              status={run?.current_status ?? null}
            />

            {error && (
              <WorkspaceError
                message={error}
                onRetry={operationError ? undefined : retryWorkspaceReads}
              />
            )}
            {isPending && <PendingOperation message={pendingMessage} />}

            <div className="grid items-start gap-10 py-8 xl:grid-cols-[minmax(0,1fr)_320px]">
              <div className="min-w-0">
                <TicketDetail
                  ticket={selectedTicket}
                  busy={investigation.isPending || jobActive}
                  onInvestigate={startTicketInvestigation}
                />
                {run && (
                  <>
                    <InvestigationFindings run={run} />
                    {run.result.proposal && (
                      <ProposalReview
                        key={run.run_id}
                        runId={run.run_id}
                        proposalId={run.result.proposal.id}
                        identity={identity}
                        currentEmployee={currentEmployee}
                        employees={personas}
                        onStatusRefresh={() =>
                          queryClient.invalidateQueries({
                            queryKey: investigationKeys.run(identity, run.run_id),
                          })
                        }
                      />
                    )}
                  </>
                )}
              </div>

              <aside className="flex flex-col gap-6 xl:sticky xl:top-6">
                <section aria-labelledby="current-stage-title">
                  <h2 id="current-stage-title" className="font-semibold">
                    Current stage
                  </h2>
                  <div className="mt-3 flex gap-3 rounded-lg border p-4">
                    {run?.current_status.code === "blocked" && !investigation.isPending ? (
                      <CircleAlert className="mt-0.5 size-5 shrink-0 text-destructive" />
                    ) : (
                      <ShieldCheck className="mt-0.5 size-5 shrink-0 text-amber-600" />
                    )}
                    <div>
                      <p className="font-medium">
                        {investigation.isPending || jobActive
                          ? "Investigation in progress"
                          : job?.status === "failed"
                            ? "Investigation failed"
                            : (run?.current_status.title ?? "Ready to investigate")}
                      </p>
                      <p className="mt-1 text-sm leading-6 text-muted-foreground">
                        {investigation.isPending || jobActive
                          ? "Reviewing records and validating the report."
                          : (run?.current_status.next_action ??
                            "Run an investigation to gather evidence and determine the next action.")}
                      </p>
                    </div>
                  </div>
                </section>

                <section className="border-t pt-6" aria-labelledby="request-details-title">
                  <h2 id="request-details-title" className="font-semibold">
                    Request details
                  </h2>
                  <dl className="mt-4 grid grid-cols-[100px_1fr] gap-x-4 gap-y-3 text-sm">
                    <dt className="text-muted-foreground">Customer</dt>
                    <dd>{selectedTicket.customer_id}</dd>
                    <dt className="text-muted-foreground">Integration</dt>
                    <dd>{selectedTicket.integration_id}</dd>
                    <dt className="text-muted-foreground">Assigned to</dt>
                    <dd>{selectedTicket.assigned_employee.name}</dd>
                  </dl>
                </section>

                <InvestigationHistory
                  history={history}
                  selectedRunId={selectedRunId}
                  busy={isPending || historyQueryResult.isFetching}
                  onOpenRun={openRun}
                  onRefresh={refreshHistory}
                />
              </aside>
            </div>
          </div>
        </main>
      )}

      {route.kind === "request" && (
        <EvidenceSheet
          open={Boolean(route.runId && route.evidenceId)}
          detail={evidenceQueryResult.data}
          error={evidenceError}
          ticketId={route.ticketId}
          runId={route.runId ?? ""}
          onClose={closeEvidence}
          onRetry={() => void evidenceQueryResult.refetch()}
        />
      )}
    </div>
  );
}

export function WorkspaceError({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <Alert variant="destructive" role="alert" className="my-5">
      <AlertTitle>Request needs attention</AlertTitle>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <AlertDescription>{message}</AlertDescription>
        {onRetry && (
          <Button variant="outline" size="sm" onClick={onRetry}>
            Try again
          </Button>
        )}
      </div>
    </Alert>
  );
}

function PendingOperation({ message }: { message: string }) {
  return (
    <Alert className="my-5">
      <LoaderCircle className="animate-spin" />
      <AlertTitle>{message}</AlertTitle>
      <AlertDescription>Results appear when the operation completes.</AlertDescription>
    </Alert>
  );
}

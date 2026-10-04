"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, RefreshCw, ShieldCheck } from "lucide-react";
import {
  requestApi,
  proposalReviewQuery,
  proposalReviewKeys,
  type Approval,
  type RequestIdentity,
  type ExecuteProposalResult,
  type VerifyDeliveryResult,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { ProposalChangeSummary } from "@/components/proposal-change-summary";
import { Field, FieldLabel, FieldDescription } from "@/components/ui/field";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertTitle, AlertDescription } from "@/components/ui/alert";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

export function ProposalReview({
  runId,
  proposalId,
  employees,
  onStatusRefresh,
}: {
  runId: string;
  proposalId: string;
  employees: { id: string; name: string; role: string }[];
  onStatusRefresh: () => Promise<void>;
}) {
  const [employee, setEmployee] = useState("emp-priya");
  const reviewerIdentity: RequestIdentity = { mode: "demo", employeeId: employee };
  const role = employees.find((item) => item.id === employee)?.role;
  const queryClient = useQueryClient();
  const url = `/api/runs/${runId}/proposals/${proposalId}`;

  // 1. Fetch with reviewer-scoped caching and cancellation.
  const reviewQuery = useQuery(
    proposalReviewQuery({ identity: reviewerIdentity, runId, proposalId }),
  );

  // 2. Refresh confirmed records after success or an uncertain failure; never retry writes.
  const approval = useMutation({
    mutationFn: () =>
      requestApi<Approval>({
        path: `${url}/approval`,
        identity: reviewerIdentity,
        options: { method: "POST" },
      }),
    retry: false,
    onSettled: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: proposalReviewKeys.proposal(runId, proposalId) }),
        queryClient.invalidateQueries({ queryKey: proposalReviewKeys.inbox(reviewerIdentity) }),
        onStatusRefresh(),
      ]),
  });
  const execution = useMutation({
    mutationFn: () =>
      requestApi<ExecuteProposalResult>({
        path: `${url}/execution`,
        identity: reviewerIdentity,
        options: { method: "POST" },
      }),
    retry: false,
    onSettled: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: ["proposal-review"] }),
        queryClient.invalidateQueries({ queryKey: ["investigation"] }),
        onStatusRefresh(),
      ]),
  });
  const verification = useMutation({
    mutationFn: () =>
      requestApi<VerifyDeliveryResult>({
        path: `${url}/verification`,
        identity: reviewerIdentity,
        options: { method: "POST" },
      }),
    retry: false,
    onSettled: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: ["proposal-review"] }),
        queryClient.invalidateQueries({ queryKey: ["investigation"] }),
        queryClient.invalidateQueries({ queryKey: ["tickets"] }),
        onStatusRefresh(),
      ]),
  });
  const busy =
    approval.isPending || execution.isPending || verification.isPending || reviewQuery.isFetching;
  const error =
    approval.error?.message ||
    execution.error?.message ||
    verification.error?.message ||
    reviewQuery.error?.message ||
    "";
  const review = !error && !busy ? reviewQuery.data : undefined;
  const proposerName =
    employees.find((item) => item.id === review?.proposal.proposed_by_employee_id)?.name ??
    review?.proposal.proposed_by_employee_id;
  function employeeName(employeeId: string) {
    return employees.find((item) => item.id === employeeId)?.name ?? employeeId;
  }

  return (
    <section className="py-7" aria-label="Proposal review" aria-live="polite">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <Field className="w-full max-w-sm">
          <FieldLabel htmlFor="reviewer">Review or execute as</FieldLabel>
          <Select
            items={employees.map((item) => ({
              value: item.id,
              label: `${item.name} · ${item.role.replaceAll("_", " ")}`,
            }))}
            disabled={busy}
            value={employee}
            onValueChange={(value) => {
              if (!value) return;
              setEmployee(value);
              approval.reset();
              execution.reset();
              verification.reset();
            }}
          >
            <SelectTrigger id="reviewer" className="w-full">
              <SelectValue placeholder="Select an option" />
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                {employees.map((item) => (
                  <SelectItem key={item.id} value={item.id}>
                    {item.name} · {item.role.replaceAll("_", " ")}
                  </SelectItem>
                ))}
              </SelectGroup>
            </SelectContent>
          </Select>
          <FieldDescription>
            Simulated identity. Access and action permissions are checked by the server.
          </FieldDescription>
        </Field>
        <Button
          variant="ghost"
          size="sm"
          disabled={busy}
          onClick={() => {
            approval.reset();
            execution.reset();
            verification.reset();
            void reviewQuery.refetch();
            void onStatusRefresh();
          }}
        >
          <RefreshCw className="size-4" />
          Refresh proposal
        </Button>
      </div>

      {error && (
        <Alert variant="destructive" className="mt-5">
          <AlertTitle>Review unavailable</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {review && (
        <div className="mt-6">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h2 className="text-lg font-semibold">Proposed change</h2>
            {!review.verification && (
              <Badge variant={review.approval ? "default" : "secondary"}>
                {review.current_status.title}
              </Badge>
            )}
          </div>

          <div className="mt-4">
            <ProposalChangeSummary
              currentEndpoint={review.proposal.current_endpoint}
              proposedEndpoint={review.proposal.proposed_endpoint}
              expectedVersion={review.proposal.expected_configuration_version}
              proposerName={proposerName ?? review.proposal.proposed_by_employee_id}
            />
          </div>

          {review.execution && !review.verification && (
            <Alert className="mt-5 border-emerald-200 bg-emerald-50/70">
              <CheckCircle2 />
              <AlertTitle>Configuration updated — delivery not yet verified</AlertTitle>
              <AlertDescription>
                <p>
                  {employeeName(review.execution.executed_by_employee_id)} ·{" "}
                  {new Date(review.execution.executed_at).toLocaleString()}
                </p>
                <p>
                  Version {review.execution.previous_configuration_version} →{" "}
                  {review.execution.resulting_configuration_version}
                </p>
                <p className="break-all">Receipt: {review.execution.id}</p>
                {execution.data && (
                  <p>
                    {execution.data.was_created
                      ? "This request updated the configuration."
                      : "Already executed; no change repeated."}
                  </p>
                )}
              </AlertDescription>
            </Alert>
          )}

          {review.verification && (
            <Alert
              className={
                review.verification.outcome === "delivered"
                  ? "mt-5 border-emerald-200 bg-emerald-50/70"
                  : "mt-5 border-amber-200 bg-amber-50/70"
              }
            >
              <CheckCircle2 />
              <AlertTitle>
                {review.verification.outcome === "delivered"
                  ? "Delivery verified"
                  : "Manual intervention required"}
              </AlertTitle>
              <AlertDescription>
                <p>{review.verification.evidence}</p>
                <p>
                  Verified by {employeeName(review.verification.verified_by_employee_id)} ·{" "}
                  {new Date(review.verification.verified_at).toLocaleString()}
                </p>
              </AlertDescription>
            </Alert>
          )}

          {!review.approval && (
            <div className="mt-5 rounded-lg border border-amber-200 bg-amber-50/70 p-4">
              <div className="flex gap-3">
                <ShieldCheck className="mt-0.5 size-5 text-amber-700" aria-hidden="true" />
                <div>
                  <p className="font-semibold text-amber-950">Independent approval required</p>
                  <p className="mt-1 text-sm leading-6 text-amber-900">
                    Production changes require an authorized reviewer who is different from the
                    proposer. Approval records the decision; it does not execute the change.
                  </p>
                </div>
              </div>
            </div>
          )}

          {review.approval ? (
            <details className="mt-5 rounded-lg border bg-muted/10 px-4 py-3 text-sm">
              <summary className="cursor-pointer font-medium">
                Approval and recovery details
              </summary>
              <div className="mt-4 space-y-4 border-t pt-4 text-muted-foreground">
                <div>
                  <p className="font-medium text-foreground">
                    Approved by {employeeName(review.approval.approved_by_employee_id)}
                  </p>
                  <p>{new Date(review.approval.created_at).toLocaleString()}</p>
                  <p className="mt-1 break-all">Approval receipt: {review.approval.id}</p>
                </div>
                <div>
                  <p className="font-medium text-foreground">Recovery plan</p>
                  <p>
                    If delivery verification fails, stop and request manual intervention. No
                    automatic rollback is authorized.
                  </p>
                </div>
                {review.execution && (
                  <p className="break-all">Execution receipt: {review.execution.id}</p>
                )}
                {review.verification && (
                  <p className="break-all">Test event: {review.verification.test_event_id}</p>
                )}
              </div>
            </details>
          ) : (
            <Alert className="mt-5">
              <AlertTitle>Recovery plan: manual intervention</AlertTitle>
              <AlertDescription>
                If delivery verification fails, stop and request manual intervention. No automatic
                rollback is authorized.
              </AlertDescription>
            </Alert>
          )}

          {!review.verification && (
            <div className="mt-6 flex flex-wrap items-center gap-3 border-t pt-5">
              <Button
                onClick={() => approval.mutate()}
                disabled={
                  busy ||
                  review.current_status.code !== "awaiting_approval" ||
                  employee === review.proposal.proposed_by_employee_id
                }
              >
                Approve this proposal
              </Button>
              {!review.execution && (
                <AlertDialog>
                  <AlertDialogTrigger
                    render={
                      <Button
                        variant="outline"
                        disabled={
                          busy ||
                          !["approval_recorded", "approval_not_required"].includes(
                            review.current_status.code,
                          ) ||
                          !["implementation_engineer", "technical_lead"].includes(role ?? "")
                        }
                      />
                    }
                  >
                    Execute change
                  </AlertDialogTrigger>
                  <AlertDialogContent>
                    <AlertDialogHeader>
                      <AlertDialogTitle>Execute {review.proposal.ticket_id}?</AlertDialogTitle>
                      <AlertDialogDescription>
                        Change {review.proposal.integration_id} ({review.proposal.environment}) from{" "}
                        {review.proposal.current_endpoint} to {review.proposal.proposed_endpoint}.
                        Recovery requires manual intervention. This does not verify delivery.
                      </AlertDialogDescription>
                    </AlertDialogHeader>
                    <AlertDialogFooter>
                      <AlertDialogCancel>Cancel</AlertDialogCancel>
                      <AlertDialogAction onClick={() => execution.mutate()}>
                        Execute change
                      </AlertDialogAction>
                    </AlertDialogFooter>
                  </AlertDialogContent>
                </AlertDialog>
              )}
              {review.execution && !review.verification && (
                <Button
                  variant="outline"
                  disabled={
                    busy || !["implementation_engineer", "technical_lead"].includes(role ?? "")
                  }
                  onClick={() => verification.mutate()}
                >
                  Verify delivery
                </Button>
              )}
              <p className="max-w-xl text-xs leading-5 text-muted-foreground">
                Execution and delivery verification are separate recorded actions. Failed or
                uncertain delivery requires manual intervention; neither execution nor rollback is
                repeated automatically.
              </p>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

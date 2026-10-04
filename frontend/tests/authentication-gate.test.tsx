import assert from "node:assert/strict";
import { test, type TestContext } from "node:test";
import { JSDOM } from "jsdom";
import React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "http://localhost:3000",
});
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  HTMLElement: dom.window.HTMLElement,
  Element: dom.window.Element,
  Node: dom.window.Node,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
});
const { render, screen, cleanup, fireEvent } = await import("@testing-library/react");
const { AuthenticationGate } = await import("../components/authentication-gate");

function mount(t: TestContext) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  t.after(() => {
    cleanup();
    client.clear();
  });
  render(
    <QueryClientProvider client={client}>
      <AuthenticationGate>
        {({ identity }) => <p>Workspace {identity.employeeId}</p>}
      </AuthenticationGate>
    </QueryClientProvider>,
  );
}

test("a visitor opens only a demo workspace despite old account session state", async (t) => {
  window.sessionStorage.setItem("switchboard-session-mode", "entra");
  t.after(() => window.sessionStorage.clear());
  let requests = 0;
  t.mock.method(globalThis, "fetch", async (path: string, options: RequestInit) => {
    requests++;
    assert.equal(path, "/api/me");
    const headers = new Headers(options.headers);
    assert.equal(headers.get("X-Demo-Persona-Id"), "emp-alex");
    assert.equal(headers.has("Authorization"), false);
    assert.equal(headers.has("X-Switchboard-Authorization"), false);
    return Response.json({
      employee_id: "emp-alex",
      name: "Alex Rivera",
      role: "implementation_engineer",
    });
  });
  mount(t);
  await screen.findByText("Workspace emp-alex");
  assert.equal(requests, 1);
  assert.equal(screen.queryByText(/Microsoft|Sign in|work account/), null);
});

test("a failed workspace stays closed and can retry without account sign-in", async (t) => {
  let failed = true;
  t.mock.method(globalThis, "fetch", async () =>
    failed
      ? Response.json({ detail: "Service unavailable" }, { status: 503 })
      : Response.json({ employee_id: "emp-alex" }),
  );
  mount(t);
  await screen.findByRole("alert");
  assert.equal(screen.queryByText(/Workspace emp-alex/), null);
  failed = false;
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  await screen.findByText("Workspace emp-alex");
});

const { ProposalReview } = await import("../components/proposal-review");

for (const proposer of ["emp-alex", "emp-priya"]) {
  test(`built-in technical lead review preserves independent approval for ${proposer}`, async (t) => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    t.after(() => {
      cleanup();
      client.clear();
    });
    t.mock.method(globalThis, "fetch", async (_path: string, options: RequestInit) => {
      assert.equal(new Headers(options.headers).get("X-Demo-Persona-Id"), "emp-priya");
      return Response.json({
        proposal: {
          id: "proposal",
          ticket_id: "CHG-1045",
          customer_id: "acme",
          integration_id: "production",
          environment: "production",
          current_endpoint: "https://old.example",
          proposed_endpoint: "https://new.example",
          expected_configuration_version: 1,
          proposed_by_employee_id: proposer,
          created_at: "2026-09-22T13:30:00Z",
        },
        approval: null,
        execution: null,
        verification: null,
        current_status: { code: "awaiting_approval", title: "Awaiting approval" },
      });
    });
    render(
      <QueryClientProvider client={client}>
        <ProposalReview
          runId="run"
          proposalId="proposal"
          employees={[
            { id: "emp-alex", name: "Alex Rivera", role: "implementation_engineer" },
            { id: "emp-priya", name: "Priya Shah", role: "technical_lead" },
            { id: "emp-ben", name: "Ben Okafor", role: "support_specialist" },
          ]}
          onStatusRefresh={async () => {}}
        />
      </QueryClientProvider>,
    );
    const button = await screen.findByRole("button", { name: "Approve this proposal" });
    assert.equal((button as HTMLButtonElement).disabled, proposer === "emp-priya");
    assert.ok(screen.getByText("Review or execute as"));
  });
}

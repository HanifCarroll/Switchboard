import assert from "node:assert/strict";
import { test } from "node:test";
import { QueryClient } from "@tanstack/react-query";
import {
  requestApi,
  demoPersonasQuery,
  historyQuery,
  investigationQuery,
  investigationEvidenceQuery,
  investigationKeys,
  ticketsQuery,
  proposalReviewQuery,
  proposalReviewKeys,
  shouldRetryReadRequest,
} from "./api.ts";

test("read queries retry while the hosted service starts", async (t) => {
  let calls = 0;
  t.mock.method(globalThis, "fetch", async () => {
    calls += 1;
    if (calls < 3) return Response.json({}, { status: 503 });
    return Response.json([{ id: "CHG-1042" }]);
  });
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: shouldRetryReadRequest, retryDelay: 0 },
    },
  });
  t.after(() => client.clear());

  const tickets = await client.fetchQuery(ticketsQuery({ mode: "demo", employeeId: "emp-alex" }));

  assert.deepEqual(tickets, [{ id: "CHG-1042" }]);
  assert.equal(calls, 3);
});

test("read recovery remains bounded", () => {
  const temporaryFailure = new TypeError("Network unavailable");

  for (let failureCount = 0; failureCount < 6; failureCount += 1) {
    assert.equal(shouldRetryReadRequest(failureCount, temporaryFailure), true);
  }
  assert.equal(shouldRetryReadRequest(6, temporaryFailure), false);
});

test("read queries do not retry authorization failures", async (t) => {
  let calls = 0;
  t.mock.method(globalThis, "fetch", async () => {
    calls += 1;
    return Response.json({ detail: "Record unavailable" }, { status: 403 });
  });
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: shouldRetryReadRequest, retryDelay: 0 },
    },
  });
  t.after(() => client.clear());

  await assert.rejects(
    client.fetchQuery(ticketsQuery({ mode: "demo", employeeId: "emp-alex" })),
    /Record unavailable/,
  );

  assert.equal(calls, 1);
});

test("ticket history is fetched and cached by employee and ticket", async (t) => {
  const calls = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    const employee = new Headers(options.headers).get("X-Demo-Persona-Id");
    calls.push([url, employee]);
    return Response.json([{ run_id: employee, ticket_id: "CHG-1042", outcome: "blocked" }]);
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  t.after(() => client.clear());

  const alex = await client.fetchQuery(
    historyQuery({ mode: "demo", employeeId: "emp-alex" }, "CHG-1042"),
  );
  const priya = await client.fetchQuery(
    historyQuery({ mode: "demo", employeeId: "emp-priya" }, "CHG-1042"),
  );
  assert.notDeepEqual(alex, priya);
  assert.deepEqual(calls, [
    ["/api/investigations?ticket_id=CHG-1042", "emp-alex"],
    ["/api/investigations?ticket_id=CHG-1042", "emp-priya"],
  ]);

  await client.invalidateQueries({
    queryKey: investigationKeys.history({ mode: "demo", employeeId: "emp-alex" }, "CHG-1042"),
  });
  assert.equal(
    client.getQueryState(
      investigationKeys.history({ mode: "demo", employeeId: "emp-alex" }, "CHG-1042"),
    ).isInvalidated,
    true,
  );
  assert.equal(
    client.getQueryState(
      investigationKeys.history({ mode: "demo", employeeId: "emp-priya" }, "CHG-1042"),
    ).isInvalidated,
    false,
  );
});

test("accessible tickets use the selected employee identity", async (t) => {
  t.mock.method(globalThis, "fetch", async (path, options) => {
    assert.equal(path, "/api/tickets");
    assert.equal(new Headers(options.headers).get("X-Demo-Persona-Id"), "emp-alex");
    return Response.json([{ id: "CHG-1042" }]);
  });

  const tickets = await ticketsQuery({ mode: "demo", employeeId: "emp-alex" }).queryFn({
    signal: AbortSignal.timeout(1000),
  });
  assert.deepEqual(tickets, [{ id: "CHG-1042" }]);
});

test("stable demo metadata remains fresh for the page session", () => {
  const identity = { mode: "demo", employeeId: "emp-alex" };

  assert.equal(demoPersonasQuery(identity).staleTime, Infinity);
});

test("canceling a run query aborts its network request", async (t) => {
  let requestSignal;
  t.mock.method(globalThis, "fetch", (_url, options) => {
    requestSignal = options.signal;
    return new Promise((_resolve, reject) => {
      options.signal.addEventListener("abort", () =>
        reject(new DOMException("Aborted", "AbortError")),
      );
    });
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  t.after(() => client.clear());
  const query = investigationQuery({ mode: "demo", employeeId: "emp-alex" }, "run-1");
  const pending = client.fetchQuery(query).catch(() => undefined);
  await client.cancelQueries({ queryKey: query.queryKey });
  await pending;
  assert.equal(requestSignal.aborted, true);
  assert.equal(client.getQueryData(query.queryKey), undefined);
});

test("unselected runs stay disabled; inaccessible runs surface errors", async (t) => {
  assert.equal(investigationQuery({ mode: "demo", employeeId: "emp-alex" }, null).enabled, false);
  assert.notDeepEqual(
    investigationKeys.run({ mode: "demo", employeeId: "emp-alex" }, "run-1"),
    investigationKeys.run({ mode: "demo", employeeId: "emp-priya" }, "run-1"),
  );
  t.mock.method(globalThis, "fetch", async () =>
    Response.json({ detail: "Record unavailable" }, { status: 403 }),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  t.after(() => client.clear());
  await assert.rejects(
    client.fetchQuery(investigationQuery({ mode: "demo", employeeId: "emp-alex" }, "run-1")),
    /Record unavailable/,
  );
});

test("investigation evidence uses the run-scoped authorized endpoint", async (t) => {
  t.mock.method(globalThis, "fetch", async (path, options) => {
    assert.equal(path, "/api/investigations/run-1/evidence/int-acme-prod");
    assert.equal(new Headers(options.headers).get("X-Demo-Persona-Id"), "emp-alex");
    return Response.json({ snapshot: { id: "int-acme-prod" }, has_changed: false });
  });

  const detail = await investigationEvidenceQuery(
    { mode: "demo", employeeId: "emp-alex" },
    "run-1",
    "int-acme-prod",
  ).queryFn({ signal: AbortSignal.timeout(1000) });

  assert.equal(detail.snapshot.id, "int-acme-prod");
});

test("proposal reviews isolate reviewers and invalidate together after approval", async (t) => {
  let approved = false;
  t.mock.method(globalThis, "fetch", async (_url, options) => {
    if (new Headers(options.headers).get("X-Demo-Persona-Id") === "emp-ben") {
      return Response.json({ detail: "Proposal unavailable" }, { status: 404 });
    }
    return Response.json({ approval: approved ? { id: "approval-1" } : null });
  });
  const client = new QueryClient();
  t.after(() => client.clear());
  const alex = proposalReviewQuery({
    identity: { mode: "demo", employeeId: "emp-alex" },
    runId: "run-1",
    proposalId: "proposal-1",
  });
  const priya = proposalReviewQuery({
    identity: { mode: "demo", employeeId: "emp-priya" },
    runId: "run-1",
    proposalId: "proposal-1",
  });
  const ben = proposalReviewQuery({
    identity: { mode: "demo", employeeId: "emp-ben" },
    runId: "run-1",
    proposalId: "proposal-1",
  });
  assert.notDeepEqual(alex.queryKey, priya.queryKey);
  assert.equal((await client.fetchQuery(alex)).approval, null);
  assert.equal((await client.fetchQuery(priya)).approval, null);
  await assert.rejects(client.fetchQuery(ben), /Proposal unavailable/);
  assert.equal(client.getQueryData(ben.queryKey), undefined);
  approved = true;
  await client.invalidateQueries({ queryKey: proposalReviewKeys.proposal("run-1", "proposal-1") });
  assert.equal(client.getQueryState(alex.queryKey).isInvalidated, true);
  assert.equal(client.getQueryState(priya.queryKey).isInvalidated, true);
  assert.equal((await client.fetchQuery(priya)).approval.id, "approval-1");
});

test("demo requests send only the selected employee identity", async (t) => {
  t.mock.method(globalThis, "fetch", async (path, options) => {
    assert.equal(path, "/api/me");
    const headers = new Headers(options.headers);
    assert.equal(headers.get("X-Demo-Persona-Id"), "emp-alex");
    assert.equal(headers.has("Authorization"), false);
    return Response.json({ employee_id: "emp-alex" });
  });
  assert.deepEqual(
    await requestApi({
      path: "/api/me",
      identity: { mode: "demo", employeeId: "emp-alex" },
    }),
    { employee_id: "emp-alex" },
  );
});

test("Entra requests acquire a token and send no simulated identity", async (t) => {
  let acquired = false;
  t.mock.method(globalThis, "fetch", async (_path, options) => {
    assert.equal(acquired, true);
    const headers = new Headers(options.headers);
    assert.equal(headers.get("Authorization"), "Bearer api-token");
    assert.equal(headers.has("X-Demo-Persona-Id"), false);
    return Response.json({ employee_id: "emp-alex" });
  });
  await requestApi({
    path: "/api/me",
    identity: {
      mode: "entra",
      accountId: "tenant:alex",
      getAccessToken: async () => {
        acquired = true;
        return "api-token";
      },
    },
  });
});

test("identity header overrides are rejected before acquiring tokens or fetching", async (t) => {
  t.mock.method(globalThis, "fetch", () => assert.fail("Must not send a request"));
  for (const identity of [
    { mode: "demo", employeeId: "emp-alex" },
    {
      mode: "entra",
      accountId: "tenant:alex",
      getAccessToken: async () => assert.fail("Must not acquire a token"),
    },
  ]) {
    for (const headers of [
      { authorization: "Bearer override" },
      new Headers({ "X-DEMO-PERSONA-ID": "emp-priya" }),
      [["Authorization", "Bearer override"]],
    ]) {
      await assert.rejects(
        requestApi({ path: "/api/me", identity, options: { headers } }),
        /identity headers/i,
      );
    }
  }
});

test("token acquisition failures never send the business request", async (t) => {
  t.mock.method(globalThis, "fetch", () => assert.fail("Must not send a request"));
  const signInError = new Error("Sign in again");
  await assert.rejects(
    requestApi({
      path: "/api/me",
      identity: {
        mode: "entra",
        accountId: "tenant:alex",
        getAccessToken: async () => {
          throw signInError;
        },
      },
    }),
    (error) => error === signInError,
  );
});

test("a rejected write is surfaced without retrying", async (t) => {
  let requests = 0;
  t.mock.method(globalThis, "fetch", async () => {
    requests++;
    return Response.json({ detail: "Execution not permitted" }, { status: 403 });
  });
  await assert.rejects(
    requestApi({
      path: "/api/runs/run/proposals/proposal/execution",
      identity: { mode: "demo", employeeId: "emp-alex" },
      options: { method: "POST" },
    }),
    /Execution not permitted/,
  );
  assert.equal(requests, 1);
});

test("Entra history is account-scoped and cannot reuse demo data", async (t) => {
  t.mock.method(globalThis, "fetch", async (_path, options) =>
    Response.json({ identity: new Headers(options.headers).get("Authorization") }),
  );
  const client = new QueryClient();
  t.after(() => client.clear());
  const alex = { mode: "entra", accountId: "alex", getAccessToken: async () => "alex-token" };
  const priya = { mode: "entra", accountId: "priya", getAccessToken: async () => "priya-token" };
  assert.deepEqual(await client.fetchQuery(historyQuery(alex, "CHG-1042")), {
    identity: "Bearer alex-token",
  });
  assert.deepEqual(await client.fetchQuery(historyQuery(priya, "CHG-1042")), {
    identity: "Bearer priya-token",
  });
  assert.notDeepEqual(
    historyQuery(alex, "CHG-1042").queryKey,
    historyQuery({ mode: "demo", employeeId: "alex" }, "CHG-1042").queryKey,
  );
});

test("queued run polling stops after completion or failure", () => {
  const query = investigationQuery({ mode: "demo", employeeId: "emp-alex" }, "run-1");
  assert.equal(query.refetchInterval({ state: { data: { status: "queued" } } }), 2000);
  assert.equal(query.refetchInterval({ state: { data: { status: "running" } } }), 2000);
  assert.equal(query.refetchInterval({ state: { data: { status: "failed" } } }), false);
  assert.equal(query.refetchInterval({ state: { data: { result: {} } } }), false);
});

test("AWS writes hash the exact body and keep application identity separate", async (t) => {
  const previous = process.env.NEXT_PUBLIC_AWS_DEPLOYMENT;
  process.env.NEXT_PUBLIC_AWS_DEPLOYMENT = "1";
  t.after(() =>
    previous === undefined
      ? delete process.env.NEXT_PUBLIC_AWS_DEPLOYMENT
      : (process.env.NEXT_PUBLIC_AWS_DEPLOYMENT = previous),
  );
  let captured;
  t.mock.method(globalThis, "fetch", async (_path, options) => {
    captured = options;
    return Response.json({ ok: true });
  });
  const body = JSON.stringify({ ticket_id: "CHG-1042" });
  await requestApi({
    path: "/api/investigations",
    identity: { mode: "entra", accountId: "account", getAccessToken: async () => "token" },
    options: { method: "POST", body },
  });
  assert.equal(captured.headers.get("Authorization"), null);
  assert.equal(captured.headers.get("X-Switchboard-Authorization"), "Bearer token");
  const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(body));
  assert.equal(captured.headers.get("X-Amz-Content-Sha256"), Buffer.from(hash).toString("hex"));
});

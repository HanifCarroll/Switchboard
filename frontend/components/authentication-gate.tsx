"use client";

import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { requestApi, type RequestIdentity, type CurrentEmployee } from "../lib/api";
import { Layers, LoaderCircle } from "lucide-react";
import { Button } from "./ui/button";

export type AuthenticatedSession = { identity: RequestIdentity };

export function AuthenticationGate({
  children,
}: {
  children: (session: AuthenticatedSession) => ReactNode;
}) {
  // Open the visitor workspace before any parallel business-data requests.
  const session = useQuery({
    queryKey: ["demo-workspace-session"],
    enabled: typeof window !== "undefined",
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    queryFn: async (): Promise<AuthenticatedSession> => {
      const identity: RequestIdentity = { mode: "demo", employeeId: "emp-alex" };
      await requestApi<CurrentEmployee>({ path: "/api/me", identity });
      return { identity };
    },
  });

  if (session.data) return children(session.data);

  return (
    <main className="flex min-h-screen items-center justify-center bg-muted/30 px-6 py-12">
      <section className="w-full max-w-md rounded-2xl border bg-background p-8 shadow-sm sm:p-10">
        <div className="mb-10 flex items-center gap-3 font-semibold">
          <Layers className="size-6" aria-hidden="true" />
          Switchboard
        </div>
        <h1 className="text-2xl font-semibold tracking-tight">Opening your demo workspace</h1>
        <p className="mt-3 text-sm leading-6 text-muted-foreground">
          Explore fictional requests using the built-in employee profiles.
        </p>
        {session.isError ? (
          <>
            <p role="alert" className="mt-6 text-sm">
              The demo is temporarily unavailable.
            </p>
            <Button className="mt-6 w-full" onClick={() => void session.refetch()}>
              Try again
            </Button>
          </>
        ) : (
          <output className="mt-8 flex items-center gap-2 text-sm text-muted-foreground">
            <LoaderCircle className="size-4 animate-spin" aria-hidden="true" />
            Preparing your workspace…
          </output>
        )}
      </section>
    </main>
  );
}

"use client";

import { useState, useSyncExternalStore, type ReactNode } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import { getAuthMode, restoreSignedInAccount, getAccessToken, signIn, signOut } from "../lib/auth";
import { requestApi, type RequestIdentity, type CurrentEmployee } from "../lib/api";
import { Layers, LoaderCircle } from "lucide-react";
import { HybridSessionChooser } from "./hybrid-session-chooser";
import { Button } from "./ui/button";

const SESSION_MODE_KEY = "switchboard-session-mode";
type SessionMode = "demo" | "entra" | "unselected";

export type AuthenticatedSession = {
  identity: RequestIdentity;
  employee: CurrentEmployee | null;
};

export type AccountActions = {
  onUseDemo: (() => void) | null;
  onUseMicrosoft: (() => void) | null;
  onSwitchAccount: (() => void) | null;
  onSignOut: (() => void) | null;
};

export function AuthenticationGate({
  children,
}: {
  children: (session: AuthenticatedSession, accountActions: AccountActions) => ReactNode;
}) {
  const authMode = getAuthMode();
  const storedSessionMode = useSyncExternalStore(
    ignoreSessionStorageChanges,
    getStoredSessionMode,
    getServerSessionMode,
  );
  const [selectedSessionMode, setSelectedSessionMode] = useState<SessionMode | null>(null);
  const sessionMode = authMode === "hybrid" ? (selectedSessionMode ?? storedSessionMode) : authMode;
  const [leaving, setLeaving] = useState(false);
  const [hasAccount, setHasAccount] = useState(false);

  // 1. Restore identity, then require the API to recognize the employee.
  const session = useQuery({
    queryKey: ["signed-in-session", sessionMode],
    enabled: typeof window !== "undefined" && sessionMode !== "unselected",
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    queryFn: async (): Promise<AuthenticatedSession | null> => {
      if (sessionMode === "demo") {
        if (process.env.NEXT_PUBLIC_AWS_DEPLOYMENT === "1") {
          await requestApi<CurrentEmployee>({
            path: "/api/me",
            identity: { mode: "demo", employeeId: "emp-alex" },
          });
        }
        return { identity: { mode: "demo", employeeId: "emp-alex" }, employee: null };
      }

      const account = await restoreSignedInAccount();
      setHasAccount(account !== null);
      if (!account) return null;

      const identity: RequestIdentity = {
        mode: "entra",
        accountId: `${account.tenantId}:${account.homeAccountId}`,
        getAccessToken: () => getAccessToken({ account }),
      };
      const employee = await requestApi<CurrentEmployee>({ path: "/api/me", identity });
      return { identity, employee };
    },
  });

  // 2. Unmount the workspace before starting an account transition.
  const authentication = useMutation({
    mutationFn: async (action: "sign-in" | "sign-out") => {
      setLeaving(true);
      if (action === "sign-in") {
        storeSessionMode("entra");
        await signIn();
      } else {
        clearSessionMode();
        await signOut();
      }
    },
    retry: false,
  });
  const error = authentication.error ?? session.error;

  function chooseSessionMode(mode: Exclude<SessionMode, "unselected">) {
    authentication.reset();
    storeSessionMode(mode);
    setLeaving(false);
  }

  function storeSessionMode(mode: Exclude<SessionMode, "unselected">) {
    if (authMode === "hybrid") window.sessionStorage.setItem(SESSION_MODE_KEY, mode);
    setSelectedSessionMode(mode);
  }

  function clearSessionMode() {
    if (authMode === "hybrid") {
      window.sessionStorage.removeItem(SESSION_MODE_KEY);
      setSelectedSessionMode("unselected");
    }
  }

  if (authMode === "hybrid" && sessionMode === "unselected" && !leaving && !authentication.error) {
    return (
      <HybridSessionChooser
        onUseDemo={() => chooseSessionMode("demo")}
        onUseMicrosoft={() => authentication.mutate("sign-in")}
      />
    );
  }

  // 3. Show the workspace only after authentication and employee access succeed.
  if (!leaving && !error && session.data) {
    return children(session.data, {
      onUseDemo:
        authMode === "hybrid" && sessionMode === "entra" ? () => chooseSessionMode("demo") : null,
      onUseMicrosoft:
        authMode === "hybrid" && sessionMode === "demo"
          ? () => authentication.mutate("sign-in")
          : null,
      onSwitchAccount: sessionMode === "entra" ? () => authentication.mutate("sign-in") : null,
      onSignOut: sessionMode === "entra" ? () => authentication.mutate("sign-out") : null,
    });
  }

  const busy = session.isPending || authentication.isPending;
  return (
    <main className="flex min-h-screen items-center justify-center bg-muted/30 px-6 py-12">
      <section
        className="w-full max-w-md rounded-2xl border bg-background p-8 shadow-sm sm:p-10"
        aria-labelledby="sign-in-title"
      >
        <div className="mb-10 flex items-center gap-3 font-semibold">
          <Layers className="size-6" aria-hidden="true" />
          Switchboard
        </div>
        <h1 id="sign-in-title" className="text-2xl font-semibold tracking-tight">
          Sign in to Switchboard
        </h1>
        <p className="mt-3 text-sm leading-6 text-muted-foreground">
          Investigate customer requests, review proposals, and manage approved changes with your
          work account.
        </p>
        {error && (
          <div
            role="alert"
            className="mt-6 rounded-lg border border-destructive/25 bg-destructive/5 p-4 text-sm"
          >
            <p className="font-medium">We couldn’t sign you in.</p>
            <p className="mt-1 leading-6 text-muted-foreground">
              Try again with your work account. If the problem continues, ask your administrator to
              check sign-in setup and your access to Switchboard.
            </p>
          </div>
        )}
        {busy ? (
          <output className="mt-8 flex items-center gap-2 text-sm text-muted-foreground">
            <LoaderCircle className="size-4 animate-spin" aria-hidden="true" />
            {session.isPending ? "Checking sign-in…" : "Connecting to Microsoft…"}
          </output>
        ) : (
          <Button
            className="mt-8 w-full"
            size="lg"
            onClick={() => authentication.mutate("sign-in")}
          >
            Sign in with Microsoft
          </Button>
        )}
        {hasAccount && (
          <Button
            className="mt-3 w-full"
            variant="ghost"
            disabled={busy}
            onClick={() => authentication.mutate("sign-out")}
          >
            Sign out
          </Button>
        )}
        {authMode === "hybrid" && sessionMode === "entra" && (
          <Button
            className="mt-3 w-full"
            variant="ghost"
            disabled={busy}
            onClick={() => chooseSessionMode("demo")}
          >
            Use demo instead
          </Button>
        )}
        <p className="mt-6 text-xs leading-5 text-muted-foreground">
          Access is limited to employees authorized for this workspace.
        </p>
      </section>
    </main>
  );
}

function getStoredSessionMode(): SessionMode {
  const stored = window.sessionStorage.getItem(SESSION_MODE_KEY);
  return stored === "demo" || stored === "entra" ? stored : "unselected";
}

function getServerSessionMode(): SessionMode {
  return "unselected";
}

function ignoreSessionStorageChanges() {
  return () => {};
}

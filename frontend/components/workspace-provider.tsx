"use client";

import { createContext, useContext, useMemo, useState, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthenticationGate } from "@/components/authentication-gate";
import {
  identityKey,
  readRetryDelay,
  shouldRetryReadRequest,
  type RequestIdentity,
} from "@/lib/api";

type WorkspaceSession = {
  identity: RequestIdentity;
  onEmployeeChange: (employeeId: string) => void;
};

const WorkspaceSessionContext = createContext<WorkspaceSession | null>(null);

function createQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: shouldRetryReadRequest,
        retryDelay: readRetryDelay,
        refetchOnWindowFocus: false,
      },
      mutations: { retry: false },
    },
  });
}

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const [authenticationClient] = useState(createQueryClient);

  return (
    <QueryClientProvider client={authenticationClient}>
      <AuthenticationGate>
        {(session) => (
          <AuthenticatedWorkspace key={JSON.stringify(identityKey(session.identity))}>
            {children}
          </AuthenticatedWorkspace>
        )}
      </AuthenticationGate>
    </QueryClientProvider>
  );
}

function AuthenticatedWorkspace({ children }: { children: ReactNode }) {
  const [queryClient] = useState(createQueryClient);
  const [demoEmployee, setDemoEmployee] = useState("emp-alex");
  const identity = useMemo<RequestIdentity>(
    () => ({ mode: "demo", employeeId: demoEmployee }),
    [demoEmployee],
  );
  const value = useMemo(
    () => ({
      identity,
      onEmployeeChange: setDemoEmployee,
    }),
    [identity],
  );

  return (
    <QueryClientProvider client={queryClient}>
      <WorkspaceSessionContext value={value}>
        <IdentityBoundary key={JSON.stringify(identityKey(identity))}>{children}</IdentityBoundary>
      </WorkspaceSessionContext>
    </QueryClientProvider>
  );
}

function IdentityBoundary({ children }: { children: ReactNode }) {
  return children;
}

export function useWorkspaceSession() {
  const session = useContext(WorkspaceSessionContext);
  if (!session) throw new Error("Workspace routes must be rendered inside WorkspaceProvider.");
  return session;
}

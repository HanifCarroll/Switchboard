import type { ReactNode } from "react";
import { CheckSquare2, Inbox } from "lucide-react";
import { DemoPersonaAvatar, DemoPersonaIndicator } from "@/components/demo-persona";
import type { DemoPersona } from "@/lib/api";

export function WorkspaceSidebar({
  activeView,
  demoPersona,
  demoPersonaSwitcher,
  onOpenWork,
  onOpenApprovals,
}: {
  activeView: "work" | "approvals";
  demoPersona: DemoPersona | null;
  demoPersonaSwitcher?: ReactNode;
  onOpenWork: () => void;
  onOpenApprovals: () => void;
}) {
  const navigation = [
    { label: "My work", icon: Inbox, active: activeView === "work", onClick: onOpenWork },
    {
      label: "Approvals",
      icon: CheckSquare2,
      active: activeView === "approvals",
      onClick: onOpenApprovals,
    },
  ];

  return (
    <aside className="flex min-h-0 w-full flex-col border-b border-sidebar-border bg-sidebar text-sidebar-foreground lg:fixed lg:inset-y-0 lg:left-0 lg:min-h-screen lg:w-60 lg:border-r lg:border-b-0">
      <div className="flex h-20 items-center gap-3 px-6 text-lg font-semibold tracking-tight">
        <span className="grid size-8 place-items-center">
          <SwitchboardMark />
        </span>
        Switchboard
      </div>

      <nav className="flex flex-col gap-1 px-3" aria-label="Workspace navigation">
        {navigation.map(({ label, icon: Icon, active, onClick }) => (
          <button
            key={label}
            type="button"
            className={`flex h-11 items-center gap-3 rounded-lg px-3 text-left text-sm transition-colors ${
              active
                ? "bg-sidebar-accent font-medium text-sidebar-accent-foreground"
                : "text-muted-foreground hover:bg-black/[0.04] hover:text-foreground"
            }`}
            aria-current={active ? "page" : undefined}
            onClick={onClick}
          >
            <Icon className="size-4" aria-hidden="true" />
            {label}
          </button>
        ))}
      </nav>

      <div className="mt-auto flex flex-col gap-3 p-3">
        <div className="border-t border-sidebar-border pt-3">
          <div className="flex items-center gap-3 rounded-lg px-2 py-2">
            <DemoPersonaAvatar persona={demoPersona} size="lg" />
            <span className="min-w-0 flex-1">
              <DemoPersonaIndicator persona={demoPersona} />
            </span>
          </div>
          {demoPersonaSwitcher}
        </div>
      </div>
    </aside>
  );
}

function SwitchboardMark() {
  return (
    <svg
      viewBox="0 0 32 32"
      className="size-8 text-blue-600"
      fill="none"
      stroke="currentColor"
      strokeWidth="3.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M25 7H12a5 5 0 0 0 0 10h8a5 5 0 0 1 0 10H7" />
      <circle cx="25" cy="7" r="2.25" fill="currentColor" stroke="none" />
      <circle cx="7" cy="27" r="2.25" fill="currentColor" stroke="none" />
    </svg>
  );
}

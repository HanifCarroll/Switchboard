import { Clock3, RefreshCw } from "lucide-react";
import type { InvestigationHistoryItem } from "@/lib/api";
import { Button } from "@/components/ui/button";

export function InvestigationHistory({
  history,
  selectedRunId,
  busy,
  onOpenRun,
  onRefresh,
}: {
  history: InvestigationHistoryItem[];
  selectedRunId: string | null;
  busy: boolean;
  onOpenRun: (id: string) => void;
  onRefresh: () => void;
}) {
  return (
    <section className="border-t pt-6" aria-labelledby="activity-title">
      <div className="flex items-center justify-between gap-3">
        <h2 id="activity-title" className="flex items-center gap-2 font-semibold">
          <Clock3 className="size-4" aria-hidden="true" />
          Investigation history
        </h2>
        <Button variant="ghost" size="icon-sm" disabled={busy} onClick={onRefresh}>
          <RefreshCw className="size-4" />
          <span className="sr-only">Refresh history</span>
        </Button>
      </div>
      <div className="mt-4 flex flex-col gap-2">
        {history.map((item) => (
          <button
            key={item.run_id}
            type="button"
            className={`rounded-lg border px-3 py-3 text-left text-sm transition-colors ${
              selectedRunId === item.run_id ? "border-blue-300 bg-blue-50" : "hover:bg-slate-50"
            }`}
            disabled={busy}
            onClick={() => onOpenRun(item.run_id)}
          >
            <span className="block font-medium capitalize">
              {(
                {
                  blocked: "Investigation blocked",
                  proposal_candidate: "Proposal prepared",
                  queued: "Investigation queued",
                  running: "Investigation running",
                  failed: "Investigation failed",
                } as Record<string, string>
              )[item.outcome] ?? item.outcome}
            </span>
            <span className="mt-1 block font-mono text-xs text-muted-foreground">
              Run {item.run_id.slice(0, 8)}
            </span>
          </button>
        ))}
        {!history.length && (
          <p className="text-sm leading-6 text-muted-foreground">
            No investigations for this request yet.
          </p>
        )}
      </div>
    </section>
  );
}

import { AlertTriangle, CheckCircle2, CircleSlash, LoaderCircle, XCircle } from "lucide-react";
import type { Outcome } from "@/api/contract";
import { cn } from "@/lib/utils";

/** The client's words for each outcome -- not the pipeline's internal statuses. */
export const OUTCOME_LABEL: Record<Outcome, string> = {
  done: "Ready",
  needs_review: "Needs review",
  processing: "Processing",
  unsupported: "Not recognised",
  failed: "Failed",
};

const STYLE: Record<Outcome, { className: string; Icon: typeof CheckCircle2 }> = {
  done: { className: "bg-ok-soft text-ok", Icon: CheckCircle2 },
  needs_review: { className: "bg-attention-soft text-attention", Icon: AlertTriangle },
  processing: { className: "bg-working-soft text-working", Icon: LoaderCircle },
  unsupported: { className: "bg-working-soft text-muted", Icon: CircleSlash },
  failed: { className: "bg-danger-soft text-danger", Icon: XCircle },
};

export function StatusBadge({ outcome, className }: { outcome: Outcome; className?: string }) {
  const { className: tone, Icon } = STYLE[outcome];
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium",
        tone,
        className,
      )}
    >
      <Icon className={cn("size-3.5", outcome === "processing" && "animate-spin")} aria-hidden />
      {OUTCOME_LABEL[outcome]}
    </span>
  );
}

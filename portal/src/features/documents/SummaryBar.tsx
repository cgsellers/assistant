import type { DocumentSummary } from "@/api/contract";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { formatMoney } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * The headline numbers for whatever the table is filtered to. Spend is shown
 * per currency and never added up across them: 8.30 EUR and 8.30 GBP are not
 * 16.60 of anything.
 */
export function SummaryBar({
  summary,
  loading,
  onShowReview,
}: {
  summary: DocumentSummary | undefined;
  loading: boolean;
  onShowReview: () => void;
}) {
  if (loading && !summary) {
    return (
      <div className="mb-6 grid grid-cols-2 gap-3 lg:grid-cols-4">
        {[0, 1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-[88px] rounded-lg" />
        ))}
      </div>
    );
  }
  if (!summary) return null;

  const review = summary.by_outcome.needs_review;
  const ready = summary.by_outcome.done;

  return (
    <div className="mb-6 grid grid-cols-2 gap-3 lg:grid-cols-4">
      <Stat label="Documents" value={summary.documents.toLocaleString()} />
      <Stat
        label="Ready"
        value={ready.toLocaleString()}
        hint={
          summary.documents
            ? `${Math.round((ready / summary.documents) * 100)}% of them`
            : undefined
        }
      />
      <button
        type="button"
        onClick={onShowReview}
        disabled={!review}
        className="text-left disabled:cursor-default"
        aria-label={`${review} need review — show them`}
      >
        <Stat
          label="Need review"
          value={review.toLocaleString()}
          hint={review ? "Show them →" : "Nothing waiting"}
          tone={review ? "attention" : undefined}
          interactive={review > 0}
        />
      </button>
      <Card className="p-4">
        <p className="text-xs font-medium text-muted">Spend</p>
        {summary.totals.length ? (
          <ul className="mt-1 space-y-0.5">
            {summary.totals.map((total) => (
              <li key={total.currency ?? "unstated"} className="tabular text-lg font-semibold">
                {formatMoney(total.amount, total.currency)}
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-1 text-lg font-semibold text-faint">—</p>
        )}
      </Card>
    </div>
  );
}

function Stat({
  label,
  value,
  hint,
  tone,
  interactive,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: "attention";
  interactive?: boolean;
}) {
  return (
    <Card
      className={cn(
        "h-full p-4 transition-colors",
        interactive && "hover:border-attention/40 hover:bg-attention-soft/40",
      )}
    >
      <p className="text-xs font-medium text-muted">{label}</p>
      <p
        className={cn(
          "tabular mt-1 text-2xl font-semibold",
          tone === "attention" && "text-attention",
        )}
      >
        {value}
      </p>
      {hint && <p className="mt-0.5 text-xs text-muted">{hint}</p>}
    </Card>
  );
}

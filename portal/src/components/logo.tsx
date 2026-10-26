import { cn } from "@/lib/utils";

/** The Ledgerline wordmark: a ruled ledger line under an L. */
export function Logo({ className }: { className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-2 font-semibold tracking-tight", className)}>
      <svg viewBox="0 0 24 24" className="size-6" aria-hidden="true">
        <rect width="24" height="24" rx="6" className="fill-brand" />
        <path d="M8 6v10h8" stroke="white" strokeWidth="2.2" fill="none" strokeLinecap="round" />
        <path
          d="M6 19.5h12"
          stroke="white"
          strokeOpacity=".55"
          strokeWidth="1.4"
          strokeLinecap="round"
        />
      </svg>
      Ledgerline
    </span>
  );
}

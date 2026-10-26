import type { ComponentProps } from "react";
import { cn } from "@/lib/utils";

export function Input({ className, ...props }: ComponentProps<"input">) {
  return (
    <input
      className={cn(
        "h-9 w-full rounded-md border border-line-strong bg-surface px-3 text-sm text-ink shadow-card",
        "placeholder:text-faint focus-visible:border-brand focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/20",
        "disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
      {...props}
    />
  );
}

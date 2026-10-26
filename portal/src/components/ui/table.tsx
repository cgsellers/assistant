import type { ComponentProps } from "react";
import { cn } from "@/lib/utils";

export function Table({ className, ...props }: ComponentProps<"table">) {
  return <table className={cn("w-full border-collapse text-sm", className)} {...props} />;
}

export function THead({ className, ...props }: ComponentProps<"thead">) {
  return <thead className={cn("border-b border-line bg-canvas/60", className)} {...props} />;
}

export function TH({ className, ...props }: ComponentProps<"th">) {
  return (
    <th
      className={cn(
        "h-10 px-4 text-left align-middle text-xs font-medium uppercase tracking-wide text-muted",
        className,
      )}
      {...props}
    />
  );
}

export function TR({ className, ...props }: ComponentProps<"tr">) {
  return <tr className={cn("border-b border-line last:border-0", className)} {...props} />;
}

export function TD({ className, ...props }: ComponentProps<"td">) {
  return <td className={cn("h-14 px-4 align-middle", className)} {...props} />;
}

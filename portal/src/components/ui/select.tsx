import * as SelectPrimitive from "@radix-ui/react-select";
import { Check, ChevronDown } from "lucide-react";
import type { ComponentProps } from "react";
import { cn } from "@/lib/utils";

/** Radix requires a non-empty value per item; this one stands for "no filter". */
export const ANY = "__any__";

export const Select = SelectPrimitive.Root;
export const SelectValue = SelectPrimitive.Value;

export function SelectTrigger({
  className,
  children,
  ...props
}: ComponentProps<typeof SelectPrimitive.Trigger>) {
  return (
    <SelectPrimitive.Trigger
      className={cn(
        "inline-flex h-9 min-w-36 items-center justify-between gap-2 rounded-md border border-line-strong bg-surface px-3 text-sm text-ink shadow-card",
        "focus-visible:border-brand focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/20 data-[placeholder]:text-faint",
        className,
      )}
      {...props}
    >
      {children}
      <SelectPrimitive.Icon>
        <ChevronDown className="size-4 text-faint" />
      </SelectPrimitive.Icon>
    </SelectPrimitive.Trigger>
  );
}

export function SelectContent({
  className,
  children,
  ...props
}: ComponentProps<typeof SelectPrimitive.Content>) {
  return (
    <SelectPrimitive.Portal>
      <SelectPrimitive.Content
        position="popper"
        sideOffset={6}
        className={cn(
          "z-50 min-w-[var(--radix-select-trigger-width)] rounded-lg border border-line bg-surface p-1 shadow-pop",
          className,
        )}
        {...props}
      >
        <SelectPrimitive.Viewport>{children}</SelectPrimitive.Viewport>
      </SelectPrimitive.Content>
    </SelectPrimitive.Portal>
  );
}

export function SelectItem({
  className,
  children,
  ...props
}: ComponentProps<typeof SelectPrimitive.Item>) {
  return (
    <SelectPrimitive.Item
      className={cn(
        "relative flex cursor-pointer select-none items-center rounded-md py-1.5 pr-8 pl-2.5 text-sm text-ink-soft outline-none",
        "data-[highlighted]:bg-working-soft data-[highlighted]:text-ink",
        className,
      )}
      {...props}
    >
      <SelectPrimitive.ItemText>{children}</SelectPrimitive.ItemText>
      <SelectPrimitive.ItemIndicator className="absolute right-2">
        <Check className="size-4 text-brand" />
      </SelectPrimitive.ItemIndicator>
    </SelectPrimitive.Item>
  );
}

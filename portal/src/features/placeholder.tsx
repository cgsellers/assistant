import { PageHeader } from "@/components/page-header";
import { Card } from "@/components/ui/card";

/** Stands in for a page still being built in step 5, so the shell can be used end to end. */
export function Placeholder({ title, step }: { title: string; step: string }) {
  return (
    <>
      <PageHeader title={title} />
      <Card className="p-10 text-center text-sm text-muted">Arrives in step {step}.</Card>
    </>
  );
}

import { Lock } from "lucide-react";
import { PageHeader } from "@/components/page-header";
import { useCan } from "@/features/auth/can";
import { useMe } from "@/features/auth/session";
import { ApiKeys } from "./ApiKeys";
import { Team } from "./Team";

export function SettingsPage() {
  const me = useMe();
  const { manage } = useCan();
  const organisation = me.data?.organisation_name ?? "your organisation";
  return (
    <>
      <PageHeader title="Settings" description={organisation} />
      {!manage && (
        <p className="mb-4 flex items-center gap-2 rounded-md border border-line bg-surface px-4 py-2.5 text-sm text-muted">
          <Lock className="size-4 shrink-0" aria-hidden /> Only an admin can change API keys and the
          team.
        </p>
      )}
      <div className="space-y-6">
        <ApiKeys manage={manage} />
        <Team manage={manage} organisation={organisation} />
      </div>
    </>
  );
}

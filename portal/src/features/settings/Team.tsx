import { UserPlus, X } from "lucide-react";
import { type FormEvent, useState } from "react";
import { toast } from "sonner";
import type { ClientRole, Invitation, Member } from "@/api/contract";
import { CopyButton } from "@/components/copy-button";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TD, TH, THead, TR } from "@/components/ui/table";
import { formatDay, formatMoment } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useChangeRole, useInvite, useMembers, useRemoveMember } from "./queries";

/** Any role a member can hold: a client's three, and the team's own "author". */
const LABEL: Record<Member["role"], string> = {
  admin: "Admin",
  member: "Member",
  viewer: "Viewer",
  author: "Author",
};

export const ROLE: Record<ClientRole, { label: string; does: string }> = {
  admin: { label: "Admin", does: "Everything a member can, plus API keys and the team." },
  member: { label: "Member", does: "Uploads documents, reviews and corrects them." },
  viewer: { label: "Viewer", does: "Sees documents and exports them. Changes nothing." },
};

const SELECT =
  "h-8 rounded-md border border-line-strong bg-surface px-2 text-sm shadow-card focus-visible:border-brand focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/20";

function Pill({ children, tone }: { children: string; tone: "brand" | "muted" }) {
  return (
    <span
      className={cn(
        "rounded-full px-2 py-px text-[11px] font-medium",
        tone === "brand" ? "bg-brand-soft text-brand" : "bg-working-soft text-muted",
      )}
    >
      {children}
    </span>
  );
}

export function Team({ manage, organisation }: { manage: boolean; organisation: string }) {
  const members = useMembers();
  const changeRole = useChangeRole();
  const remove = useRemoveMember();
  const [inviting, setInviting] = useState(false);
  const [removing, setRemoving] = useState<Member | null>(null);

  return (
    <Card className="overflow-hidden">
      <div className="flex flex-wrap items-start justify-between gap-4 border-b border-line px-5 py-4">
        <div>
          <h2 className="font-semibold">Team</h2>
          <p className="mt-0.5 text-sm text-muted">The people at {organisation} who can sign in.</p>
        </div>
        {manage && (
          <Button onClick={() => setInviting(true)}>
            <UserPlus /> Invite someone
          </Button>
        )}
      </div>

      {members.isPending ? (
        <div className="space-y-2 p-5">
          <Skeleton className="h-5 w-2/3" />
          <Skeleton className="h-5 w-1/2" />
        </div>
      ) : members.isError ? (
        <p role="alert" className="p-5 text-sm text-danger">
          The team could not be loaded: {members.error.message}
        </p>
      ) : (
        <Table aria-label="Team">
          <THead>
            <tr>
              <TH>Person</TH>
              <TH className="w-40">Role</TH>
              <TH className="w-56">Last seen</TH>
              {manage && (
                <TH className="w-14">
                  <span className="sr-only">Remove</span>
                </TH>
              )}
            </tr>
          </THead>
          <tbody>
            {members.data.map((member) => (
              <TR key={member.id}>
                <TD>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">{member.email}</span>
                    {member.you && <Pill tone="brand">You</Pill>}
                    {member.status === "invited" && <Pill tone="muted">Invited</Pill>}
                  </div>
                </TD>
                <TD>
                  {manage && !member.you ? (
                    <select
                      aria-label={`Role of ${member.email}`}
                      className={SELECT}
                      value={member.role}
                      disabled={changeRole.isPending}
                      onChange={(event) =>
                        changeRole.mutate(
                          { id: member.id, role: event.target.value as ClientRole },
                          {
                            onSuccess: (changed) =>
                              toast.success(
                                `${changed.email} is now ${LABEL[changed.role].toLowerCase()}.`,
                              ),
                            onError: (error) => toast.error(error.message),
                          },
                        )
                      }
                    >
                      {(Object.keys(ROLE) as ClientRole[]).map((role) => (
                        <option key={role} value={role}>
                          {ROLE[role].label}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <span className="text-sm">{LABEL[member.role]}</span>
                  )}
                </TD>
                <TD className="tabular whitespace-nowrap text-sm">
                  {member.you ? (
                    <span className="text-muted">Now</span>
                  ) : member.last_seen_at ? (
                    formatMoment(member.last_seen_at)
                  ) : (
                    <span className="text-faint">Not yet</span>
                  )}
                </TD>
                {manage && (
                  <TD className="text-right">
                    {!member.you && (
                      <Button
                        variant="ghost"
                        size="icon"
                        className="size-8 text-muted hover:text-danger"
                        aria-label={`Remove ${member.email}`}
                        onClick={() => setRemoving(member)}
                      >
                        <X />
                      </Button>
                    )}
                  </TD>
                )}
              </TR>
            ))}
          </tbody>
        </Table>
      )}

      <dl className="grid gap-x-6 gap-y-1 border-t border-line bg-canvas/50 px-5 py-3 text-xs sm:grid-cols-3">
        {(Object.keys(ROLE) as ClientRole[]).map((role) => (
          <div key={role}>
            <dt className="font-medium text-ink-soft">{ROLE[role].label}</dt>
            <dd className="text-muted">{ROLE[role].does}</dd>
          </div>
        ))}
      </dl>

      <Invite open={inviting} onClose={() => setInviting(false)} />
      <ConfirmDialog
        open={removing !== null}
        title={`Remove ${removing?.email ?? ""}?`}
        confirm="Remove"
        busy={remove.isPending}
        onCancel={() => setRemoving(null)}
        onConfirm={() =>
          removing &&
          remove.mutate(removing.id, {
            onSuccess: () => toast.success(`${removing.email} can no longer sign in.`),
            onError: (error) => toast.error(error.message),
            onSettled: () => setRemoving(null),
          })
        }
      >
        They're signed out and can't sign in again. What they corrected or approved stays on record.
      </ConfirmDialog>
    </Card>
  );
}

/** Invite someone, then hand over the link: Ledgerline does not send email yet. */
function Invite({ open, onClose }: { open: boolean; onClose: () => void }) {
  const invite = useInvite();
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<ClientRole>("member");
  const [sent, setSent] = useState<Invitation | null>(null);

  function close() {
    onClose();
    setTimeout(() => {
      setEmail("");
      setRole("member");
      setSent(null);
      invite.reset();
    }, 200);
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    invite.mutate({ email: email.trim(), role }, { onSuccess: setSent });
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => !next && close()}
      title={sent ? "Send them this link" : "Invite someone"}
      description={
        sent
          ? `Ledgerline doesn't send emails yet, so pass this link to ${sent.member.email} yourself. It works once, until ${formatDay(sent.expires_at)}.`
          : "They'll choose their own password when they accept."
      }
    >
      {sent ? (
        <div className="space-y-4">
          <div className="flex items-center gap-2">
            <Input
              readOnly
              aria-label="Invitation link"
              value={sent.invite_url}
              onFocus={(event) => event.target.select()}
              className="min-w-0 flex-1 bg-canvas font-mono text-xs"
            />
            <CopyButton text={sent.invite_url} label="Copy link" />
          </div>
          <div className="flex justify-end">
            <Button onClick={close}>Done</Button>
          </div>
        </div>
      ) : (
        <form onSubmit={submit} className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="invite-email">Email</Label>
            <Input
              id="invite-email"
              type="email"
              autoFocus
              required
              placeholder="name@acme.com"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
          </div>
          <fieldset className="space-y-2">
            <legend className="mb-1.5 text-sm font-medium">Role</legend>
            {(Object.keys(ROLE) as ClientRole[]).map((option) => (
              <label
                key={option}
                className={cn(
                  "flex cursor-pointer items-start gap-3 rounded-md border px-3 py-2.5 text-sm transition-colors",
                  role === option ? "border-brand bg-brand-soft/40" : "border-line hover:bg-canvas",
                )}
              >
                <input
                  type="radio"
                  name="role"
                  value={option}
                  checked={role === option}
                  onChange={() => setRole(option)}
                  className="mt-0.5 accent-[var(--color-brand)]"
                />
                <span>
                  <span className="font-medium">{ROLE[option].label}</span>
                  <span className="block text-muted">{ROLE[option].does}</span>
                </span>
              </label>
            ))}
          </fieldset>
          {invite.error && (
            <p role="alert" className="text-sm text-danger">
              {invite.error.message}
            </p>
          )}
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={close}>
              Cancel
            </Button>
            <Button type="submit" disabled={!email.trim() || invite.isPending}>
              Create invitation
            </Button>
          </div>
        </form>
      )}
    </Dialog>
  );
}

import { KeyRound, Plus } from "lucide-react";
import { type FormEvent, useState } from "react";
import { toast } from "sonner";
import type { ApiKeyRow, CreatedKey } from "@/api/contract";
import { CopyButton } from "@/components/copy-button";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TD, TH, THead, TR } from "@/components/ui/table";
import { formatMoment } from "@/lib/format";
import { useCreateKey, useKeys, useRevokeKey } from "./queries";

/** Where a client's system sends documents: the API, on the portal's own origin. */
const API = () => `${window.location.origin}/api`;

export function ApiKeys({ manage }: { manage: boolean }) {
  const keys = useKeys();
  const revoke = useRevokeKey();
  const [creating, setCreating] = useState(false);
  const [revoking, setRevoking] = useState<ApiKeyRow | null>(null);

  return (
    <Card className="overflow-hidden">
      <div className="flex flex-wrap items-start justify-between gap-4 border-b border-line px-5 py-4">
        <div>
          <h2 className="font-semibold">API keys</h2>
          <p className="mt-0.5 max-w-xl text-sm text-muted">
            For your own systems, such as an ERP or a script, to send documents and fetch what was
            read. Each key sees only your organisation's documents. Make one per system, so each can
            be revoked on its own.
          </p>
        </div>
        {manage && (
          <Button onClick={() => setCreating(true)}>
            <Plus /> Create key
          </Button>
        )}
      </div>

      {keys.isPending ? (
        <div className="space-y-2 p-5">
          <Skeleton className="h-5 w-2/3" />
          <Skeleton className="h-5 w-1/2" />
        </div>
      ) : keys.isError ? (
        <p role="alert" className="p-5 text-sm text-danger">
          The keys could not be loaded: {keys.error.message}
        </p>
      ) : keys.data.length === 0 ? (
        <div className="flex flex-col items-center px-6 py-10 text-center text-sm text-muted">
          <KeyRound className="mb-2 size-5 text-faint" />
          No keys yet.
        </div>
      ) : (
        <Table aria-label="API keys">
          <THead>
            <tr>
              <TH>Name</TH>
              <TH>Key</TH>
              <TH>Created</TH>
              <TH>Last used</TH>
              {manage && (
                <TH className="w-24">
                  <span className="sr-only">Revoke</span>
                </TH>
              )}
            </tr>
          </THead>
          <tbody>
            {keys.data.map((key) => (
              <TR key={key.id}>
                <TD className="font-medium">{key.name}</TD>
                <TD>
                  <code className="rounded bg-working-soft px-1.5 py-0.5 font-mono text-xs">
                    {key.prefix}…
                  </code>
                </TD>
                <TD className="text-sm">
                  <div className="tabular">{formatMoment(key.created_at)}</div>
                  <div className="text-xs text-muted">{key.created_by ?? "by Ledgerline"}</div>
                </TD>
                <TD className="tabular text-sm">
                  {key.last_used_at ? (
                    formatMoment(key.last_used_at)
                  ) : (
                    <span className="text-faint">Never</span>
                  )}
                </TD>
                {manage && (
                  <TD className="text-right">
                    <Button
                      variant="ghost"
                      size="sm"
                      className="text-danger hover:bg-danger-soft hover:text-danger"
                      aria-label={`Revoke ${key.name}`}
                      onClick={() => setRevoking(key)}
                    >
                      Revoke
                    </Button>
                  </TD>
                )}
              </TR>
            ))}
          </tbody>
        </Table>
      )}

      <CreateKey open={creating} onClose={() => setCreating(false)} />
      <ConfirmDialog
        open={revoking !== null}
        title={`Revoke "${revoking?.name ?? ""}"?`}
        confirm="Revoke key"
        busy={revoke.isPending}
        onCancel={() => setRevoking(null)}
        onConfirm={() =>
          revoking &&
          revoke.mutate(revoking.id, {
            onSuccess: () => toast.success(`"${revoking.name}" is revoked.`),
            onError: (error) => toast.error(error.message),
            onSettled: () => setRevoking(null),
          })
        }
      >
        Anything using it stops working at once. This can't be undone: create a new key to replace
        it.
      </ConfirmDialog>
    </Card>
  );
}

/** Name a key, then show its secret: once, with how to use it. */
function CreateKey({ open, onClose }: { open: boolean; onClose: () => void }) {
  const create = useCreateKey();
  const [name, setName] = useState("");
  const [created, setCreated] = useState<CreatedKey | null>(null);

  function close() {
    onClose();
    // After the dialog has gone, so the secret never flashes back into the form.
    setTimeout(() => {
      setName("");
      setCreated(null);
      create.reset();
    }, 200);
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    create.mutate(name.trim(), { onSuccess: setCreated });
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => !next && close()}
      title={created ? "Your new key" : "Create an API key"}
      description={created ? `For ${created.key.name}.` : "Name it after what will use it."}
    >
      {created ? (
        <div className="space-y-4">
          <div className="rounded-md border border-attention/25 bg-attention-soft/60 px-3 py-2 text-sm text-ink-soft">
            Copy it now. It won't be shown again: Ledgerline keeps only a fingerprint of it, never
            the key itself.
          </div>
          <div className="flex items-center gap-2">
            <Input
              readOnly
              aria-label="The new key"
              value={created.secret}
              onFocus={(event) => event.target.select()}
              className="min-w-0 flex-1 bg-canvas font-mono text-xs"
            />
            <CopyButton text={created.secret} />
          </div>
          <div>
            <p className="mb-1.5 text-sm font-medium">Send a document with it</p>
            <pre className="overflow-x-auto rounded-md bg-ink px-3 py-2.5 font-mono text-xs leading-relaxed text-white">
              {`curl -H "Authorization: Bearer ${created.secret}" \\\n     -F file=@invoice.pdf \\\n     ${API()}/documents`}
            </pre>
          </div>
          <div className="flex justify-end">
            <Button onClick={close}>Done</Button>
          </div>
        </div>
      ) : (
        <form onSubmit={submit} className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="key-name">Name</Label>
            <Input
              id="key-name"
              autoFocus
              required
              maxLength={200}
              placeholder="NetSuite production"
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </div>
          {create.error && (
            <p role="alert" className="text-sm text-danger">
              {create.error.message}
            </p>
          )}
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={close}>
              Cancel
            </Button>
            <Button type="submit" disabled={!name.trim() || create.isPending}>
              Create key
            </Button>
          </div>
        </form>
      )}
    </Dialog>
  );
}

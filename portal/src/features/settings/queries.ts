import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ApiKeyRow, ClientRole, CreatedKey, Invitation, Member } from "@/api/contract";
import { getJson, sendJson } from "@/lib/http";

export function useKeys() {
  return useQuery({
    queryKey: ["settings", "keys"],
    queryFn: () => getJson<ApiKeyRow[]>("/settings/keys"),
  });
}

export function useMembers() {
  return useQuery({
    queryKey: ["settings", "members"],
    queryFn: () => getJson<Member[]>("/settings/members"),
  });
}

/** A change to settings, which then refetches the list it changed. */
function useChange<T, V>(list: "keys" | "members", send: (variables: V) => Promise<T>) {
  const queries = useQueryClient();
  return useMutation({
    mutationFn: send,
    onSuccess: () => queries.invalidateQueries({ queryKey: ["settings", list] }),
  });
}

export const useCreateKey = () =>
  useChange("keys", (name: string) => sendJson<CreatedKey>("POST", "/settings/keys", { name }));

export const useRevokeKey = () =>
  useChange("keys", (id: string) =>
    sendJson<void>("DELETE", `/settings/keys/${encodeURIComponent(id)}`),
  );

export const useInvite = () =>
  useChange("members", (invite: { email: string; role: ClientRole }) =>
    sendJson<Invitation>("POST", "/settings/members", invite),
  );

export const useChangeRole = () =>
  useChange("members", ({ id, role }: { id: string; role: ClientRole }) =>
    sendJson<Member>("PATCH", `/settings/members/${encodeURIComponent(id)}`, { role }),
  );

export const useRemoveMember = () =>
  useChange("members", (id: string) =>
    sendJson<void>("DELETE", `/settings/members/${encodeURIComponent(id)}`),
  );

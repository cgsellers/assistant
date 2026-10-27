import { HttpResponse, http } from "msw";
import type { ApiKeyRow, ClientRole, CreatedKey, Invitation, Member } from "@/api/contract";
import { hasCsrf, isSignedIn, MOCK_USER, mockRole } from "./user";

/**
 * /settings: Acme's API keys and team, as the backend must answer for them.
 * Reading takes any signed-in person; every change takes an admin and the
 * CSRF token. An organisation is never left without an admin.
 */

const ROLES: ClientRole[] = ["admin", "member", "viewer"];

function startingKeys(): ApiKeyRow[] {
  return [
    {
      id: "key-1",
      name: "NetSuite production",
      prefix: "lk_9fQ2xLmA",
      created_at: "2026-09-02T10:14:00Z",
      created_by: "bo@acme.test",
      last_used_at: "2026-10-26T17:41:00Z",
    },
    {
      id: "key-2",
      name: "Expense script (Ana)",
      prefix: "lk_T7vHc0pe",
      created_at: "2026-10-01T08:30:00Z",
      created_by: null,
      last_used_at: null,
    },
  ];
}

function startingMembers(): Member[] {
  return [
    {
      id: "m-1",
      email: MOCK_USER.name,
      role: "admin",
      status: "active",
      last_seen_at: null,
      you: true,
    },
    {
      id: "m-2",
      email: "ana.ruiz@acme.test",
      role: "member",
      status: "active",
      last_seen_at: "2026-10-26T15:02:00Z",
      you: false,
    },
    {
      id: "m-3",
      email: "audit@external-accountants.test",
      role: "viewer",
      status: "active",
      last_seen_at: "2026-10-12T09:45:00Z",
      you: false,
    },
  ];
}

let keys = startingKeys();
let members = startingMembers();
let counter = 0;

export function resetSettings() {
  keys = startingKeys();
  members = startingMembers();
  counter = 0;
}

const refuse = (status: number, detail: string) => HttpResponse.json({ detail }, { status });

/** Why the caller may not change settings, as a response; null if they may. */
function mayChange(request: Request) {
  if (!isSignedIn()) return refuse(401, "not signed in");
  if (!hasCsrf(request)) return refuse(403, "missing or wrong CSRF token");
  if (mockRole() !== "admin") return refuse(403, "Only an admin can change this.");
  return null;
}

/** "lk_" and 43 characters, as core/auth.py makes them. */
function secret(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return `lk_${btoa(String.fromCharCode(...bytes))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "")}`;
}

const admins = () => members.filter((m) => m.role === "admin" && m.status === "active");

export const settingsHandlers = [
  http.get("/api/settings/keys", () =>
    isSignedIn() ? HttpResponse.json(keys) : refuse(401, "not signed in"),
  ),

  http.post("/api/settings/keys", async ({ request }) => {
    const refused = mayChange(request);
    if (refused) return refused;
    const name = String(((await request.json()) as { name?: unknown }).name ?? "").trim();
    if (!name) return refuse(422, "Give the key a name, so it can be recognised later.");
    if (name.length > 200) return refuse(422, "Keep the name under 200 characters.");
    const value = secret();
    counter += 1;
    const key: ApiKeyRow = {
      id: `key-new-${counter}`,
      name,
      prefix: value.slice(0, 11),
      created_at: new Date().toISOString(),
      created_by: MOCK_USER.name,
      last_used_at: null,
    };
    keys = [key, ...keys];
    const body: CreatedKey = { key, secret: value };
    return HttpResponse.json(body, { status: 201 });
  }),

  http.delete("/api/settings/keys/:id", ({ request, params }) => {
    const refused = mayChange(request);
    if (refused) return refused;
    if (!keys.some((k) => k.id === params.id)) return refuse(404, "No such key");
    keys = keys.filter((k) => k.id !== params.id);
    return new HttpResponse(null, { status: 204 });
  }),

  http.get("/api/settings/members", () =>
    isSignedIn() ? HttpResponse.json(members) : refuse(401, "not signed in"),
  ),

  http.post("/api/settings/members", async ({ request }) => {
    const refused = mayChange(request);
    if (refused) return refused;
    const body = (await request.json()) as { email?: unknown; role?: unknown };
    const email = String(body.email ?? "")
      .trim()
      .toLowerCase();
    const role = body.role as ClientRole;
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email))
      return refuse(422, "That doesn't look like an email address.");
    if (!ROLES.includes(role)) return refuse(422, "Choose admin, member or viewer.");
    if (members.some((m) => m.email === email))
      return refuse(409, `${email} is already in the team.`);
    counter += 1;
    const member: Member = {
      id: `m-new-${counter}`,
      email,
      role,
      status: "invited",
      last_seen_at: null,
      you: false,
    };
    members = [...members, member];
    const invitation: Invitation = {
      member,
      invite_url: `${new URL("/accept-invite", location.origin).href}?token=mock-${counter}-${crypto.randomUUID()}`,
      expires_at: new Date(Date.now() + 7 * 86_400_000).toISOString(),
    };
    return HttpResponse.json(invitation, { status: 201 });
  }),

  http.patch("/api/settings/members/:id", async ({ request, params }) => {
    const refused = mayChange(request);
    if (refused) return refused;
    const member = members.find((m) => m.id === params.id);
    if (!member) return refuse(404, "No such member");
    const role = ((await request.json()) as { role?: unknown }).role as ClientRole;
    if (!ROLES.includes(role)) return refuse(422, "Choose admin, member or viewer.");
    if (
      member.role === "admin" &&
      role !== "admin" &&
      admins().length === 1 &&
      member.status === "active"
    ) {
      return refuse(409, "Acme Ltd needs at least one admin. Make someone else an admin first.");
    }
    members = members.map((m) => (m.id === member.id ? { ...m, role } : m));
    return HttpResponse.json(members.find((m) => m.id === member.id));
  }),

  http.delete("/api/settings/members/:id", ({ request, params }) => {
    const refused = mayChange(request);
    if (refused) return refused;
    const member = members.find((m) => m.id === params.id);
    if (!member) return refuse(404, "No such member");
    if (member.you) return refuse(409, "You can't remove yourself. Ask another admin.");
    if (member.role === "admin" && admins().length === 1 && member.status === "active") {
      return refuse(409, "Acme Ltd needs at least one admin.");
    }
    members = members.filter((m) => m.id !== member.id);
    return new HttpResponse(null, { status: 204 });
  }),
];

import type { ClientRole } from "@/api/contract";
import { useMe } from "./session";

/**
 * What the signed-in person may do, by role (see ClientRole). The portal hides
 * what they cannot do; the API refuses it regardless.
 */
export function can(role: string | undefined) {
  const r = role as ClientRole | "author" | undefined;
  return {
    /** Upload, correct and approve documents: everyone but a viewer (and the team's authors too). */
    work: r !== undefined && r !== "viewer",
    /** API keys and the team. */
    manage: r === "admin",
  };
}

export function useCan() {
  return can(useMe().data?.role);
}

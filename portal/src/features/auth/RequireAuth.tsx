import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router";
import { Spinner } from "@/components/ui/spinner";
import { useMe } from "./session";

/** Signed-in pages only: anyone else is sent to sign in, then brought back. */
export function RequireAuth({ children }: { children: ReactNode }) {
  const me = useMe();
  const location = useLocation();

  if (me.isPending) {
    return (
      <div className="grid min-h-screen place-items-center text-muted">
        <Spinner className="size-5" />
      </div>
    );
  }
  if (!me.data) {
    const next = encodeURIComponent(location.pathname + location.search);
    return <Navigate to={`/sign-in?next=${next}`} replace />;
  }
  return children;
}

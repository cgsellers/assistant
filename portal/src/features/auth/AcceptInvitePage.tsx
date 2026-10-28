import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useEffect, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router";
import { Logo } from "@/components/logo";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { api, setCsrfToken, unwrap } from "@/lib/api";
import { formatDay } from "@/lib/format";

const ROLE_SAYS: Record<string, string> = {
  admin: "an admin",
  member: "a member",
  viewer: "a viewer",
  author: "an author",
};

/**
 * Where an invitation link lands: who it is for, then a password, then signed
 * in. The token is taken out of the address bar as soon as it is read, so it
 * does not linger in history or travel on as a referrer.
 */
export function AcceptInvitePage() {
  const location = useLocation();
  const navigate = useNavigate();
  const queries = useQueryClient();
  const [token] = useState(
    () =>
      new URLSearchParams(location.search).get("token") ??
      (location.state as { token?: string } | null)?.token ??
      "",
  );
  const [password, setPassword] = useState("");

  useEffect(() => {
    if (new URLSearchParams(location.search).has("token")) {
      navigate("/accept-invite", { replace: true, state: { token } });
    }
  }, [location.search, navigate, token]);

  const offer = useQuery({
    queryKey: ["invitation", token],
    queryFn: async () => unwrap(await api.POST("/auth/invitation", { body: { token } })),
    enabled: Boolean(token),
    retry: false,
    staleTime: Number.POSITIVE_INFINITY,
  });

  const join = useMutation({
    mutationFn: async () =>
      unwrap(await api.POST("/auth/invitation/accept", { body: { token, password } })),
    onSuccess: (me) => {
      setCsrfToken(me.csrf_token ?? null);
      queries.setQueryData(["me"], me);
      navigate("/documents", { replace: true });
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    join.mutate();
  }

  return (
    <main className="grid min-h-screen place-items-center px-4">
      <div className="w-full max-w-sm">
        <div className="mb-8 flex justify-center">
          <Logo className="text-lg" />
        </div>
        <Card className="p-8">
          {!token || offer.isError ? (
            <div role="alert">
              <h1 className="text-xl font-semibold tracking-tight">
                This link doesn't work any more
              </h1>
              <p className="mt-2 text-sm text-muted">
                Invitation links work once, for a week, and a newer invitation replaces an older
                one. Ask whoever invited you for a new link.
              </p>
              <Button variant="secondary" className="mt-6 w-full" asChild>
                <Link to="/sign-in">Sign in instead</Link>
              </Button>
            </div>
          ) : offer.isPending ? (
            <div aria-busy="true" className="space-y-3">
              <Skeleton className="h-7 w-48" />
              <Skeleton className="h-4 w-full" />
              <Skeleton className="h-9 w-full" />
            </div>
          ) : (
            <>
              <h1 className="text-xl font-semibold tracking-tight">
                Join {offer.data.organisation_name}
              </h1>
              <p className="mt-1 text-sm text-muted">
                You're invited as {ROLE_SAYS[offer.data.role] ?? offer.data.role}, as{" "}
                <span className="font-medium text-ink">{offer.data.email}</span>. Choose a password
                to finish.
              </p>
              <form className="mt-6 space-y-4" onSubmit={submit}>
                <div className="space-y-1.5">
                  <Label htmlFor="password">Password</Label>
                  <Input
                    id="password"
                    type="password"
                    autoComplete="new-password"
                    required
                    autoFocus
                    minLength={12}
                    aria-describedby="password-hint"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                  />
                  <p id="password-hint" className="text-xs text-muted">
                    At least 12 characters. A few unrelated words make a good one.
                  </p>
                </div>
                {join.error && (
                  <p
                    role="alert"
                    className="rounded-md bg-danger-soft px-3 py-2 text-sm text-danger"
                  >
                    {join.error.message}
                  </p>
                )}
                <Button type="submit" className="w-full" disabled={join.isPending}>
                  {join.isPending && <Spinner />}
                  Join {offer.data.organisation_name}
                </Button>
              </form>
              <p className="mt-4 text-center text-xs text-faint">
                This link works until {formatDay(offer.data.expires_at)}.
              </p>
            </>
          )}
        </Card>
      </div>
    </main>
  );
}

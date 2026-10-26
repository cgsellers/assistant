import { type FormEvent, useState } from "react";
import { Navigate, useNavigate, useSearchParams } from "react-router";
import { Logo } from "@/components/logo";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Spinner } from "@/components/ui/spinner";
import { useMe, useSignIn } from "./session";

/** Only same-site paths: `next` comes from the URL, so it is not trusted. */
function safeNext(next: string | null) {
  return next?.startsWith("/") && !next.startsWith("//") ? next : "/documents";
}

export function SignInPage() {
  const me = useMe();
  const signIn = useSignIn();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  if (me.data) {
    return <Navigate to={safeNext(params.get("next"))} replace />;
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    signIn.mutate(
      { email, password },
      { onSuccess: () => navigate(safeNext(params.get("next")), { replace: true }) },
    );
  }

  return (
    <main className="grid min-h-screen place-items-center px-4">
      <div className="w-full max-w-sm">
        <div className="mb-8 flex justify-center">
          <Logo className="text-lg" />
        </div>
        <Card className="p-8">
          <h1 className="text-xl font-semibold tracking-tight">Sign in</h1>
          <p className="mt-1 text-sm text-muted">Your documents, and what was read from them.</p>
          <form className="mt-6 space-y-4" onSubmit={submit}>
            <div className="space-y-1.5">
              <Label htmlFor="email">Email</Label>
              <Input
                id="email"
                type="email"
                autoComplete="username"
                required
                autoFocus
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="password">Password</Label>
              <Input
                id="password"
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
            {signIn.error && (
              <p role="alert" className="rounded-md bg-danger-soft px-3 py-2 text-sm text-danger">
                {signIn.error.message}
              </p>
            )}
            <Button type="submit" className="w-full" disabled={signIn.isPending}>
              {signIn.isPending && <Spinner />}
              Sign in
            </Button>
          </form>
        </Card>
        <p className="mt-6 text-center text-xs text-faint">
          Trouble signing in? Ask your account's administrator.
        </p>
      </div>
    </main>
  );
}

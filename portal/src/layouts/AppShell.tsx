import { Building2, ChevronDown, LogOut } from "lucide-react";
import { NavLink, Outlet, useNavigate } from "react-router";
import { Logo } from "@/components/logo";
import { MockBadge } from "@/components/mock-notice";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useMe, useSignOut } from "@/features/auth/session";
import { cn } from "@/lib/utils";

const NAV = [
  { to: "/documents", label: "Documents" },
  { to: "/upload", label: "Upload" },
  { to: "/settings", label: "Settings" },
];

export function AppShell() {
  const me = useMe();
  const signOut = useSignOut();
  const navigate = useNavigate();

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-40 border-b border-line bg-surface/90 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-7xl items-center gap-8 px-6">
          <div className="flex items-center gap-2">
            <NavLink to="/documents" aria-label="Ledgerline home">
              <Logo />
            </NavLink>
            <MockBadge />
          </div>
          <nav className="flex items-center gap-1">
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) =>
                  cn(
                    "rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
                    isActive ? "bg-working-soft text-ink" : "text-muted hover:text-ink",
                  )
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
          <div className="ml-auto">
            <DropdownMenu>
              <DropdownMenuTrigger className="flex items-center gap-2 rounded-md px-2 py-1.5 text-sm text-ink-soft hover:bg-working-soft">
                <Building2 className="size-4 text-muted" />
                <span className="font-medium">
                  {me.data?.organisation_name ?? "Your organisation"}
                </span>
                <ChevronDown className="size-3.5 text-faint" />
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuLabel>Signed in as {me.data?.name}</DropdownMenuLabel>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  onSelect={() =>
                    signOut.mutate(undefined, {
                      onSettled: () => navigate("/sign-in", { replace: true }),
                    })
                  }
                >
                  <LogOut /> Sign out
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-6 py-8">
        <Outlet />
      </main>
    </div>
  );
}

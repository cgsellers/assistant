import { createBrowserRouter, Navigate } from "react-router";
import { RequireAuth } from "@/features/auth/RequireAuth";
import { SignInPage } from "@/features/auth/SignInPage";
import { Placeholder } from "@/features/placeholder";
import { AppShell } from "@/layouts/AppShell";

export const routes = [
  { path: "/sign-in", element: <SignInPage /> },
  {
    element: (
      <RequireAuth>
        <AppShell />
      </RequireAuth>
    ),
    children: [
      { index: true, element: <Navigate to="/documents" replace /> },
      { path: "documents", element: <Placeholder title="Documents" step="5.2" /> },
      { path: "documents/:id", element: <Placeholder title="Document" step="5.3" /> },
      { path: "upload", element: <Placeholder title="Upload" step="5.5" /> },
      { path: "settings", element: <Placeholder title="Settings" step="5.6" /> },
    ],
  },
  { path: "*", element: <Navigate to="/documents" replace /> },
];

export const router = createBrowserRouter(routes);

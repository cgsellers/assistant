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
      {
        path: "documents",
        lazy: () =>
          import("@/features/documents/DocumentsPage").then((m) => ({
            Component: m.DocumentsPage,
          })),
      },
      {
        path: "documents/:id",
        lazy: () =>
          import("@/features/documents/DocumentPage").then((m) => ({ Component: m.DocumentPage })),
      },
      {
        path: "upload",
        lazy: () =>
          import("@/features/upload/UploadPage").then((m) => ({ Component: m.UploadPage })),
      },
      { path: "settings", element: <Placeholder title="Settings" step="5.6" /> },
    ],
  },
  { path: "*", element: <Navigate to="/documents" replace /> },
];

export const router = createBrowserRouter(routes);

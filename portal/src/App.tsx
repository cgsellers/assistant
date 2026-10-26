import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "react-router";
import { Toaster } from "sonner";
import { ApiError } from "@/lib/api";
import { router } from "@/router";

const queries = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      // A 4xx will not change on a retry; a dropped connection might.
      retry: (failures, error) =>
        !(error instanceof ApiError && error.status < 500) && failures < 2,
    },
  },
});

export function App() {
  return (
    <QueryClientProvider client={queries}>
      <RouterProvider router={router} />
      <Toaster position="bottom-right" toastOptions={{ className: "text-sm" }} />
    </QueryClientProvider>
  );
}

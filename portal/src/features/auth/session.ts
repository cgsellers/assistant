import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { components } from "@/api/schema";
import { ApiError, api, setCsrfToken, unwrap } from "@/lib/api";

export type Me = components["schemas"]["MeOut"];

const ME = ["me"] as const;

/**
 * Who is signed in, or null. A 401 is an answer here, not an error: it means
 * signed out. The CSRF token rides along on every answer, so a reloaded page
 * recovers it without anything being kept in storage a script could read.
 */
export function useMe() {
  return useQuery({
    queryKey: ME,
    queryFn: async (): Promise<Me | null> => {
      const result = await api.GET("/auth/me");
      if (result.response.status === 401) {
        setCsrfToken(null);
        return null;
      }
      const me = unwrap(result);
      setCsrfToken(me.csrf_token ?? null);
      return me;
    },
    staleTime: 5 * 60_000,
    retry: false,
  });
}

export function useSignIn() {
  const queries = useQueryClient();
  return useMutation({
    mutationFn: async (credentials: { email: string; password: string }) => {
      const result = await api.POST("/auth/login", { body: credentials });
      if (result.response.status === 401) {
        throw new ApiError(401, "That email and password don't match an account.");
      }
      return unwrap(result);
    },
    onSuccess: (me) => {
      setCsrfToken(me.csrf_token ?? null);
      queries.setQueryData(ME, me);
    },
  });
}

export function useSignOut() {
  const queries = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      await api.POST("/auth/logout");
    },
    onSettled: () => {
      setCsrfToken(null);
      queries.clear();
      queries.setQueryData(ME, null);
    },
  });
}

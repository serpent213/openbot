import { QueryClient } from "@tanstack/react-query";

export function createQueryClient(): QueryClient {
  // Queries can wait/refetch on reconnect. Operator mutations must never queue
  // silently for later delivery or retry an uncertain result.
  return new QueryClient({ defaultOptions: {
    queries: { staleTime: 5_000, retry: 1 },
    mutations: { networkMode: "always", retry: false },
  } });
}

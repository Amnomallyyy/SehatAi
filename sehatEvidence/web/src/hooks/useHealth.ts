import { useQuery } from "@tanstack/react-query";
import { getHealth } from "../lib/api";

/** Periodic re-check (previously fetched once on page load only, so a
 * server that went down mid-session gave no reconnect indication). */
export function useHealth() {
  return useQuery({
    queryKey: ["health"],
    queryFn: getHealth,
    refetchInterval: 30_000,
    retry: 1,
  });
}

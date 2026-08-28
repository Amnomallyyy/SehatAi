import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { clearHistory, deleteHistoryItem, getHistory, getHistoryItem } from "../lib/api";

export function useHistoryList(params: { limit?: number; offset?: number } = {}) {
  return useQuery({
    queryKey: ["history", params],
    queryFn: () => getHistory(params),
  });
}

export function useHistoryItem(id: string | undefined) {
  return useQuery({
    queryKey: ["history", "item", id],
    queryFn: () => getHistoryItem(id as string),
    enabled: !!id,
  });
}

export function useDeleteHistoryItem() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteHistoryItem(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["history"] }),
  });
}

export function useClearHistory() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => clearHistory(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["history"] }),
  });
}

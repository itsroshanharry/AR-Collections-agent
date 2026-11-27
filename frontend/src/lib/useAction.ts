import { useMutation, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "../api/client";
import { useToast } from "../components/toast";

/** Query keys that hold client-side state rather than a server read. */
export const LOCAL_KEYS = new Set(["lastIngest"]);

/** A write to the API: on success, toast what happened and refetch every query, since
 *  one action (a payment, a resolved task) moves numbers on several screens at once. */
export function useAction<TArgs, TResult>(
  fn: (args: TArgs) => Promise<TResult>,
  options: { success?: (result: TResult) => string; onDone?: (result: TResult) => void } = {},
) {
  const queryClient = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: fn,
    onSuccess: async (result) => {
      options.onDone?.(result);
      if (options.success) toast("good", options.success(result));
      // Client-held results (the last cash batch) have no server query to refetch.
      await queryClient.invalidateQueries({
        predicate: (q) => !LOCAL_KEYS.has(String(q.queryKey[0])),
      });
    },
    onError: (error) => {
      toast("bad", error instanceof ApiError || error instanceof Error ? error.message : "Request failed");
    },
  });
}

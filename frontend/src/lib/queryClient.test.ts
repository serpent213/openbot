import { onlineManager } from "@tanstack/react-query";
import { expect, it, vi } from "vitest";
import { createQueryClient } from "./queryClient";
it("fails an offline mutation immediately and never replays it when online", async () => {
  const client = createQueryClient();
  client.mount();
  const fn = vi.fn(async () => { throw new TypeError("offline"); });
  try {
    onlineManager.setOnline(false);
    const mutation = client.getMutationCache().build(client, { mutationFn: fn });
    await expect(mutation.execute(undefined)).rejects.toThrow("offline");
    expect(mutation.state.isPaused).toBe(false);
    onlineManager.setOnline(true);
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(fn).toHaveBeenCalledOnce();
  } finally { onlineManager.setOnline(true); client.unmount(); client.clear(); }
});

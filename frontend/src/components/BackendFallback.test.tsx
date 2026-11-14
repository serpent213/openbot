// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import BackendFallback from "./BackendFallback";
import { backendAvailableEvent, backendUnavailableEvent } from "../api/errors";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
let cleanup = () => {};
afterEach(() => { cleanup(); vi.useRealTimers(); });
it("preserves the thread on failures, refreshes on resume, and clears the banner on success", async () => {
  vi.useFakeTimers();
  const qc = new QueryClient();
  const refresh = vi.spyOn(qc, "invalidateQueries");
  const node = document.createElement("div");
  const root = createRoot(node);
  function Path() { return <span>{useLocation().pathname}</span>; }
  await act(async () => root.render(<QueryClientProvider client={qc}><MemoryRouter initialEntries={["/threads/t1"]}><BackendFallback><Path /></BackendFallback></MemoryRouter></QueryClientProvider>));
  cleanup = () => { act(() => root.unmount()); qc.clear(); };
  await act(async () => window.dispatchEvent(new Event(backendUnavailableEvent)));
  expect(node.textContent).toContain("/threads/t1");
  expect(node.querySelector('[role="status"]')).not.toBeNull();
  await act(async () => {
    window.dispatchEvent(new Event("online"));
    document.dispatchEvent(new Event("visibilitychange"));
    vi.advanceTimersByTime(110);
  });
  expect(refresh).toHaveBeenCalledTimes(1);
  await act(async () => window.dispatchEvent(new Event(backendAvailableEvent)));
  expect(node.querySelector('[role="status"]')).toBeNull();
  expect(node.textContent).toBe("/threads/t1");
});

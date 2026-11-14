// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import PwaUpdates from "./PwaUpdates";
(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
let cleanup = () => {};
afterEach(() => { cleanup(); vi.unstubAllEnvs(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
it("offers an update without auto-activation; cancellation preserves unsaved work", async () => {
  vi.stubEnv("PROD", true);
  vi.stubGlobal("isSecureContext", true);
  const waiting = { state: "installed", postMessage: vi.fn() };
  const registration = Object.assign(new EventTarget(), { waiting, installing: null, update: vi.fn() });
  const serviceWorker = Object.assign(new EventTarget(), { controller: {}, register: vi.fn(async () => registration) });
  vi.stubGlobal("navigator", { serviceWorker });
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  const container = document.createElement("div");
  const root = createRoot(container);
  cleanup = () => { act(() => root.unmount()); };
  await act(async () => root.render(<PwaUpdates />));
  expect(serviceWorker.register).toHaveBeenCalledWith("/sw.js", { scope: "/", updateViaCache: "none" });
  expect(container.textContent).toContain("update available");
  expect(waiting.postMessage).not.toHaveBeenCalled();
  await act(async () => container.querySelector("button")!.click());
  expect(waiting.postMessage).not.toHaveBeenCalled();
  confirm.mockReturnValue(true);
  await act(async () => container.querySelector("button")!.click());
  expect(waiting.postMessage).toHaveBeenCalledWith({ type: "ACTIVATE_UPDATE" });
});
it("does not register in development", async () => {
  vi.stubEnv("PROD", false);
  const register = vi.fn();
  vi.stubGlobal("navigator", { serviceWorker: { register } });
  const root = createRoot(document.createElement("div"));
  cleanup = () => { act(() => root.unmount()); };
  await act(async () => root.render(<PwaUpdates />));
  expect(register).not.toHaveBeenCalled();
});

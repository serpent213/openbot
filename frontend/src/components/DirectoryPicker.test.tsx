// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { DirectoryListing, ExecutionTarget } from "../api/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// A fake filesystem the way the backend would report it: workspace root plus the user's home.
const tree: Record<string, DirectoryListing> = {
  ".": { path: ".", parent: null, entries: [{ name: "backend", path: "backend" }, { name: "frontend", path: "frontend" }] },
  frontend: { path: "frontend", parent: ".", entries: [{ name: "src", path: "frontend/src" }] },
  "~": { path: "~", parent: null, entries: [{ name: "work", path: "~/work" }] },
  "~/work": { path: "~/work", parent: "~", entries: [{ name: "core-web", path: "~/work/core-web" }, { name: "openbot", path: "~/work/openbot" }] },
  "~/work/core-web": { path: "~/work/core-web", parent: "~/work", entries: [] },
};
const listDirectories = vi.fn(async (path: string, _target?: ExecutionTarget) => {
  const hit = tree[path];
  if (!hit) throw new Error(`{"detail":"working_directory does not exist: ${path}"}`);
  return hit;
});
vi.mock("../api/client", () => ({ Api: { listDirectories: (p: string, target?: ExecutionTarget) => listDirectories(p, target) } }));

import DirectoryPicker from "./DirectoryPicker";
import { initialPickerPath } from "../lib/workingDirectory";

async function flush() { await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); }
const byText = (el: HTMLElement, text: string) => [...el.querySelectorAll("button")].find((b) => b.textContent?.trim() === text)!;

describe("DirectoryPicker", () => {
  let root: Root; let el: HTMLDivElement;
  afterEach(() => { act(() => root.unmount()); el.remove(); listDirectories.mockClear(); });

  async function mount(initialPath: string, target?: ExecutionTarget) {
    const onSelect = vi.fn(); const onClose = vi.fn();
    el = document.createElement("div"); document.body.appendChild(el); root = createRoot(el);
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    await act(async () => { root.render(<QueryClientProvider client={qc}><DirectoryPicker initialPath={initialPath} target={target} onSelect={onSelect} onClose={onClose} /></QueryClientProvider>); });
    await flush();
    return { onSelect, onClose };
  }

  it("starts at the workspace root and lists its folders", async () => {
    await mount(".");
    expect(el.querySelector('[data-testid="picker-path"]')?.textContent).toBe(". (workspace root)");
    expect(el.textContent).toContain("backend");
    expect(el.textContent).toContain("frontend");
    expect((byText(el, "↑ Up") as HTMLButtonElement).disabled).toBe(true);
  });

  it("walks into the home directory and selects a sibling of the workspace", async () => {
    const { onSelect } = await mount(".");
    await act(async () => { byText(el, "Home").click(); }); await flush();
    await act(async () => { byText(el, "work").click(); }); await flush();
    await act(async () => { byText(el, "core-web").click(); }); await flush();
    expect(el.querySelector('[data-testid="picker-path"]')?.textContent).toBe("~/work/core-web");
    expect(el.textContent).toContain("No subfolders.");
    await act(async () => { byText(el, "Use this folder").click(); });
    expect(onSelect).toHaveBeenCalledWith("~/work/core-web");
  });

  it("routes sandbox browsing and hides the unsupported home tab", async () => {
    await mount(".", { sandbox_id: "development" });
    expect(listDirectories).toHaveBeenCalledWith(".", { sandbox_id: "development" });
    expect(byText(el, "Home")).toBeUndefined();
    await act(async () => { byText(el, "frontend").click(); }); await flush();
    expect(listDirectories).toHaveBeenLastCalledWith("frontend", { sandbox_id: "development" });
  });

  it("sends an explicit host choice for host browsing", async () => {
    await mount(".", { host_execution: true });
    expect(listDirectories).toHaveBeenCalledWith(".", { host_execution: true });
    expect(byText(el, "Home")).toBeDefined();
  });

  it("goes up one level to the reported parent", async () => {
    await mount("frontend");
    await act(async () => { byText(el, "↑ Up").click(); }); await flush();
    expect(el.querySelector('[data-testid="picker-path"]')?.textContent).toBe(". (workspace root)");
  });

  it("shows the backend's error for an unreadable path", async () => {
    await mount("nope");
    expect(el.textContent).toContain("working_directory does not exist: nope");
    expect((byText(el, "Use this folder") as HTMLButtonElement).disabled).toBe(true);
  });

  it("opens at the typed path only when it is a valid working directory", () => {
    expect(initialPickerPath("")).toBe(".");
    expect(initialPickerPath("frontend/src")).toBe("frontend/src");
    expect(initialPickerPath("~/work/core-web")).toBe("~/work/core-web");
    expect(initialPickerPath("/absolute")).toBe(".");
  });
});

// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Api } from "../api/client";
import type { SandboxInventory } from "../api/types";
import ThreadsPage from "./ThreadsPage";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("../api/client", () => ({ Api: { listThreads: vi.fn(), listBots: vi.fn(), listSandboxes: vi.fn(), createThread: vi.fn(), listDirectories: vi.fn() } }));
let root: Root;
let el: HTMLDivElement;
let inventory: SandboxInventory;
const flush = async () => { await act(async () => { await new Promise((resolve) => setTimeout(resolve, 10)); }); };
const button = (text: string) => [...el.querySelectorAll("button")].find((item) => item.textContent === text)!;
const select = () => el.querySelector<HTMLSelectElement>('select[aria-label="Execution location"]')!;
async function choose(value: string) {
  await act(async () => { select().value = value; select().dispatchEvent(new Event("change", { bubbles: true })); });
}
async function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => { root.render(<QueryClientProvider client={client}><MemoryRouter><ThreadsPage /></MemoryRouter></QueryClientProvider>); });
  await flush();
}
beforeEach(() => {
  el = document.createElement("div"); document.body.appendChild(el); root = createRoot(el);
  inventory = { enabled: true, default_sandbox_id: "a", host_execution_allowed: false, sandboxes: [
    { id: "a", name: "Development", resource: "slot1", state: "active", share_global_memory: false },
    { id: "b", name: "Research", resource: "slot2", state: "active", share_global_memory: false },
    { id: "c", name: "Retired", resource: "slot3", state: "quarantined", share_global_memory: false },
  ] };
  vi.mocked(Api.listSandboxes).mockImplementation(async () => inventory);
  vi.mocked(Api.listThreads).mockResolvedValue([]);
  vi.mocked(Api.listBots).mockResolvedValue([]);
  vi.mocked(Api.createThread).mockResolvedValue({ id: "created" } as never);
  vi.mocked(Api.listDirectories).mockResolvedValue({ path: ".", parent: null, entries: [] });
});
afterEach(() => { act(() => root.unmount()); el.remove(); vi.resetAllMocks(); });

it("selects the configured default and sends a different explicitly selected sandbox", async () => {
  await mount();
  expect(select().value).toBe("a");
  expect(select().textContent).toContain("Development (default)");
  expect(select().querySelector<HTMLOptionElement>('option[value="c"]')!.disabled).toBe(true);
  expect(select().textContent).not.toContain("Host");
  await choose("b");
  await act(async () => { button("Create thread").click(); });
  expect(Api.createThread).toHaveBeenCalledWith(expect.objectContaining({ sandbox_id: "b" }));
  expect(vi.mocked(Api.createThread).mock.calls[0][0]).not.toHaveProperty("host_execution");
});

it("requires a choice when no default exists; never selects Host automatically", async () => {
  inventory.default_sandbox_id = null;
  inventory.host_execution_allowed = true;
  await mount();
  expect(select().value).toBe("");
  expect(button("Create thread").disabled).toBe(true);
  await choose("host");
  expect(el.querySelector('[role="note"]')?.className).toContain("text-danger");
  expect(el.textContent).toContain("not necessarily Unix root");
  await act(async () => { button("Create thread").click(); });
  expect(Api.createThread).toHaveBeenCalledWith(expect.objectContaining({ host_execution: true }));
  expect(vi.mocked(Api.createThread).mock.calls[0][0]).not.toHaveProperty("sandbox_id");
});

it("routes directory browsing to the selected sandbox", async () => {
  await mount(); await choose("b");
  await act(async () => { el.querySelector<HTMLButtonElement>('[aria-label="Browse for directory"]')!.click(); });
  await flush();
  expect(Api.listDirectories).toHaveBeenCalledWith(".", { sandbox_id: "b" });
  expect(el.querySelector('[role="tablist"]')?.textContent).not.toContain("Home");
});

it("preserves legacy creation when sandboxing is disabled and labels it unrestricted", async () => {
  inventory.enabled = false;
  await mount();
  expect(select()).toBeNull();
  expect(el.textContent).toContain("Host — unrestricted");
  await act(async () => { button("Create thread").click(); });
  expect(Api.createThread).toHaveBeenCalledWith({ title: "", handles: [] });
});

it("does not create or browse when inventory fails", async () => {
  vi.mocked(Api.listSandboxes).mockRejectedValue(new Error("Inventory unavailable"));
  await mount();
  expect(el.textContent).toContain("Inventory unavailable");
  expect(button("Create thread").disabled).toBe(true);
  expect(el.querySelector<HTMLButtonElement>('[aria-label="Browse for directory"]')!.disabled).toBe(true);
  expect(Api.createThread).not.toHaveBeenCalled();
});

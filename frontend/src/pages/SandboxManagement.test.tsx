// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { Api } from "../api/client";
import type { Sandbox, SandboxCredential } from "../api/types";
import SettingsPage from "./SettingsPage";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: Root; let el: HTMLDivElement; let qc: QueryClient;
let box: Sandbox;
let credentials: SandboxCredential[];
async function tick() { await act(async () => { await new Promise((r) => setTimeout(r, 10)); }); }
async function until(test: () => boolean) {
  const stop = Date.now() + 2000;
  while (!test()) { if (Date.now() > stop) throw new Error(`Timed out: ${el.textContent}`); await tick(); }
}
const button = (label: string) => [...el.querySelectorAll("button")].find((b) => b.textContent === label)!;
async function input(label: string, text: string) {
  const node = el.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`)!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(node, text);
    node.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
async function mount(section = "sandboxes") {
  await act(async () => root.render(<QueryClientProvider client={qc}><MemoryRouter initialEntries={[`/settings/${section}`]}><SettingsPage /></MemoryRouter></QueryClientProvider>));
  await tick();
}
beforeEach(() => {
  box = { id: "box", name: "Development", resource: "openbot-sbx1", state: "active", configured: true, share_global_memory: false };
  credentials = [{ id: "key", name: "Exa", kind: "env", target: "EXA_API_KEY", is_set: true, sandbox_ids: [] }];
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  vi.spyOn(Api, "listSandboxes").mockImplementation(async () => ({ enabled: true, sandboxes: [box] }));
  vi.spyOn(Api, "listSandboxCredentials").mockImplementation(async () => credentials);
  vi.spyOn(Api, "getSettings").mockResolvedValue([]);
  vi.spyOn(Api, "getDatabaseLocation").mockResolvedValue({ location: "test-only" });
  vi.spyOn(Api, "getProviders").mockResolvedValue({ providers: [], embedding_model: "none", embeddings_configured: false });
  vi.spyOn(Api, "listTools").mockResolvedValue({ tools: [], errors: [] });
  vi.spyOn(Api, "listActors").mockResolvedValue([]);
  vi.spyOn(Api, "listMcpServers").mockResolvedValue([]);
  vi.spyOn(Api, "listMcpCatalog").mockResolvedValue([]);
  el = document.createElement("div"); document.body.append(el); root = createRoot(el);
});
afterEach(async () => { await act(async () => root.unmount()); qc.clear(); el.remove(); vi.restoreAllMocks(); });

it("uses linked settings sections and only fetches the selected section", async () => {
  await mount("general");
  expect(el.querySelector('nav[aria-label="Settings sections"]')).not.toBeNull();
  expect(Api.getDatabaseLocation).toHaveBeenCalled();
  expect(Api.listMcpServers).not.toHaveBeenCalled();
  expect(Api.listSandboxCredentials).not.toHaveBeenCalled();
  const link = el.querySelector<HTMLAnchorElement>('a[href="/settings/sandboxes"]')!;
  await act(async () => link.click());
  await until(() => el.textContent!.includes("Account/resource: openbot-sbx1"));
  expect(link.getAttribute("aria-current")).toBe("page");
  expect(el.textContent).not.toContain("Active database location");
  const picker = el.querySelector<HTMLSelectElement>('select[aria-label="Settings section"]')!;
  await act(async () => { picker.value = "credentials"; picker.dispatchEvent(new Event("change", { bubbles: true })); });
  await until(() => !!button("Add credential"));
});

it("renames metadata without changing a resource or binding", async () => {
  const save = vi.spyOn(Api, "updateSandbox").mockImplementation(async (_id, values) => (box = { ...box, ...values }));
  await mount();
  await input("Name for openbot-sbx1", "Projects");
  await act(async () => button("Rename").click());
  await until(() => save.mock.calls.length === 1);
  expect(save).toHaveBeenCalledWith("box", { name: "Projects" });
  expect(box.id).toBe("box"); expect(box.resource).toBe("openbot-sbx1");
});

it("credential selection is a draft until explicitly saved", async () => {
  const attach = vi.spyOn(Api, "setSandboxCredentials").mockImplementation(async (_id, ids) => {
    credentials = credentials.map((row) => ({ ...row, sandbox_ids: ids.includes(row.id) ? ["box"] : [] }));
    return { notice: "future runs" };
  });
  await mount();
  await act(async () => el.querySelector<HTMLInputElement>('input[aria-label="Attach Exa to Development"]')!.click());
  expect(attach).not.toHaveBeenCalled();
  await act(async () => button("Save attachments").click());
  await until(() => attach.mock.calls.length === 1);
  expect(attach).toHaveBeenCalledWith("box", ["key"]);
});

it("creates a write-only environment secret and clears the input after success", async () => {
  const create = vi.spyOn(Api, "createSandboxCredential").mockResolvedValue(credentials[0]);
  await mount("credentials");
  await input("Credential name", "Exa"); await input("Credential target", "EXA_API_KEY"); await input("Secret value", "fake-secret");
  expect(el.querySelector<HTMLInputElement>('input[aria-label="Secret value"]')!.type).toBe("password");
  await act(async () => el.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  await until(() => el.querySelector<HTMLInputElement>('input[aria-label="Secret value"]')!.value === "");
  expect(create).toHaveBeenCalledWith({ name: "Exa", target: "EXA_API_KEY", kind: "env", value: "fake-secret" });
  expect(el.textContent).not.toContain("fake-secret");
  expect(localStorage.length).toBe(0);
});

it("requires the exact sandbox name and does not pretend failed clearing succeeded", async () => {
  const clear = vi.spyOn(Api, "clearSandboxHome").mockRejectedValue(new Error("Cleanup unconfirmed; quarantined"));
  await mount();
  await act(async () => button("Clear home, keep dotfiles").click());
  expect(button("Confirm clear home").disabled).toBe(true);
  await input("Confirm sandbox name", "wrong");
  expect(button("Confirm clear home").disabled).toBe(true);
  await input("Confirm sandbox name", "Development");
  await act(async () => button("Confirm clear home").click());
  await until(() => el.textContent!.includes("Cleanup unconfirmed"));
  expect(clear).toHaveBeenCalledExactlyOnceWith("box", { keep_dotfiles: true, confirmation: "Development", generation: 1 });
  expect(el.textContent).not.toContain("Home cleared.");
  expect(button("Confirm clear home").disabled).toBe(true);
});

it("does not offer usable clear actions on unavailable or quarantined workers", async () => {
  box.configured = false;
  await mount();
  expect(button("Clear home, including dotfiles").disabled).toBe(true);
  expect(el.textContent).toContain("unavailable");
});

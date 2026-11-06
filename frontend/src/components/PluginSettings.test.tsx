// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as client from "../api/client";
import PluginSettings from "./PluginSettings";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let el: HTMLDivElement, root: Root, qc: QueryClient;
const documentValue = { revision: 7, locked: ["enabled"], error: null, value: { enabled: false, access: "off" },
  schema: { properties: { enabled: { type: "boolean", title: "Enabled" }, access: { type: "string", title: "Access", enum: ["off", "ro", "rw"] } } } };
const until = async (check: () => boolean) => { for (let n = 0; n < 30 && !check(); n++) await act(async () => { await new Promise(r => setTimeout(r, 5)); }); expect(check()).toBe(true); };
beforeEach(() => {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  el = document.createElement("div"); document.body.append(el); root = createRoot(el);
  vi.spyOn(client.Api, "listPlugins").mockResolvedValue([{ id: "example", version: "0.1", settings_scopes: ["global", "agent"] }]);
});
afterEach(async () => { await act(async () => root.unmount()); qc.clear(); el.remove(); vi.restoreAllMocks(); });

it("locks deployment fields and includes the loaded revision when saving bot settings", async () => {
  const request = vi.spyOn(client, "api").mockImplementation(async (_path, options) => options?.method ? { ...documentValue, revision: 8 } : documentValue);
  await act(async () => root.render(<QueryClientProvider client={qc}><PluginSettings actorId="bot-id" /></QueryClientProvider>));
  await until(() => !!el.querySelector("select"));
  expect(el.querySelector<HTMLInputElement>('input[aria-label="Enabled"]')!.disabled).toBe(true);
  expect(el.textContent).toContain("Locked by deployment");
  await act(async () => { const select = el.querySelector("select")!; select.value = "rw"; select.dispatchEvent(new Event("change", { bubbles: true })); });
  await act(async () => { [...el.querySelectorAll("button")].find(b => b.textContent === "Save plugin settings")!.click(); });
  expect(request).toHaveBeenCalledWith("/bots/bot-id/plugin-settings/example", { method: "PATCH", json: { expected_revision: 7, value: { access: "rw" } } });
});

it("reports a stale write and requires reload rather than silently retrying", async () => {
  vi.spyOn(client, "api").mockImplementation(async (_path, options) => { if (options?.method) throw new Error("settings changed; reload before saving"); return documentValue; });
  await act(async () => root.render(<QueryClientProvider client={qc}><PluginSettings /></QueryClientProvider>));
  await until(() => !!el.querySelector("select"));
  await act(async () => { const select = el.querySelector("select")!; select.value = "ro"; select.dispatchEvent(new Event("change", { bubbles: true })); });
  await act(async () => { [...el.querySelectorAll("button")].find(b => b.textContent === "Save plugin settings")!.click(); });
  await until(() => el.textContent!.includes("reload before saving"));
});

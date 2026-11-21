// @vitest-environment jsdom
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import ScheduledPage from "./ScheduledPage";
import { describeCron, presetCron } from "../lib/cronPresets";
import type { RecurringSchedule } from "../api/client";

const fake = vi.hoisted(() => ({ api: {} as Record<string, (...args: any[]) => any> }));
vi.mock("../api/client", () => ({ Api: fake.api }));

let root: Root;
let el: HTMLDivElement;
let qc: QueryClient;
let schedules: RecurringSchedule[];
const text = () => el.textContent ?? "";
async function until(condition: () => boolean) {
  const deadline = Date.now() + 2000;
  while (!condition()) {
    if (Date.now() > deadline) throw new Error(`Timed out: ${text()}`);
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)); });
  }
}
const button = (label: string) => {
  const found = [...el.querySelectorAll("button")].find(b => b.textContent === label);
  if (!found) throw new Error(`Missing button ${label}`);
  return found;
};
function setValue(element: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement, value: string) {
  const proto = Object.getPrototypeOf(element);
  Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(element, value);
  element.dispatchEvent(new Event(element instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
}
const schedule = (over: Partial<RecurringSchedule> = {}): RecurringSchedule => ({
  id: "r1", thread_id: "t1", sender_actor_id: "b1", to_handles: [], content: "Standup?", cron: "0 9 * * 1-5",
  timezone: "Europe/Berlin", status: "active", next_due_at: "2026-11-23T08:00:00Z", last_fired_at: null,
  last_message_id: null, skipped: 0, created_at: "", updated_at: "", ...over,
});

beforeEach(() => {
  schedules = [schedule()];
  fake.api.listThreads = vi.fn(async () => [{ id: "t1", title: "Daily" }]);
  fake.api.listActors = vi.fn(async () => [{ id: "b1", handle: "eng", name: "Engineer" }]);
  fake.api.listScheduled = vi.fn(async () => []);
  fake.api.listRecurring = vi.fn(async () => schedules);
  fake.api.previewRecurring = vi.fn(async (cron: string) => {
    if (cron.startsWith("bad")) throw new Error("expected five fields");
    return { cron, timezone: "Europe/Berlin", next: ["2026-11-23T08:00:00Z"] };
  });
  fake.api.createRecurring = vi.fn(async () => schedule({ id: "r2" }));
  fake.api.updateRecurring = vi.fn(async (_id: string, body: { paused: boolean }) => {
    schedules = [schedule({ status: body.paused ? "paused" : "active", next_due_at: body.paused ? null : "2026-11-23T08:00:00Z" })];
    return schedules[0];
  });
  fake.api.deleteRecurring = vi.fn(async () => { schedules = []; });
  el = document.createElement("div"); document.body.append(el); root = createRoot(el);
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
});
afterEach(() => { act(() => root.unmount()); qc.clear(); el.remove(); vi.unstubAllGlobals(); });

async function render() {
  await act(async () => root.render(<QueryClientProvider client={qc}><MemoryRouter><ScheduledPage /></MemoryRouter></QueryClientProvider>));
  await until(() => text().includes("Standup?"));
}

it("turns presets into cron rules and back into words", () => {
  expect(presetCron("weekdays", "09:05", 1, "")).toBe("5 9 * * 1-5");
  expect(presetCron("weekly", "18:30", 5, "")).toBe("30 18 * * 5");
  expect(presetCron("hourly", "00:15", 1, "")).toBe("15 * * * *");
  expect(presetCron("custom", "09:00", 1, " */20 8-18 * * * ")).toBe("*/20 8-18 * * *");
  expect(describeCron("5 9 * * 1-5")).toBe("Weekdays at 09:05");
  expect(describeCron("30 18 * * 5")).toBe("Fridays at 18:30");
  expect(describeCron("0 7 * * *")).toBe("Every day at 07:00");
  expect(describeCron("*/20 8-18 * * *")).toBe("*/20 8-18 * * *");
});

it("lists schedules with owner and next run, pauses and deletes after confirmation", async () => {
  await render();
  expect(text()).toContain("Weekdays at 09:00");
  expect(text()).toContain("by @eng in Daily");
  await act(async () => button("Pause").click());
  await until(() => text().includes("(paused)"));
  expect(fake.api.updateRecurring).toHaveBeenCalledWith("r1", { paused: true });
  vi.stubGlobal("confirm", vi.fn(() => true));
  await act(async () => button("Delete").click());
  await until(() => text().includes("No recurring messages yet."));
  expect(fake.api.deleteRecurring).toHaveBeenCalledWith("r1", expect.anything());
});

it("creates a schedule from a preset and shows rule errors from the preview", async () => {
  await render();
  const form = el.querySelector('form[aria-label="Create recurring message"]')!;
  await act(async () => {
    setValue(form.querySelector("select")!, "t1");
    setValue(form.querySelector("textarea")!, "Coffee?");
  });
  await until(() => text().includes("next "));
  await act(async () => button("Create schedule").click());
  await until(() => vi.mocked(fake.api.createRecurring).mock.calls.length === 1);
  expect(vi.mocked(fake.api.createRecurring).mock.calls[0][0]).toMatchObject({ thread_id: "t1", content: "Coffee?", cron: "0 9 * * 1-5" });
  const repeat = [...form.querySelectorAll("select")].find(s => [...s.options].some(o => o.value === "custom"))!;
  await act(async () => setValue(repeat, "custom"));
  const cronInput = [...form.querySelectorAll("input")].find(i => i.value === "0 9 * * 1-5")!;
  await act(async () => setValue(cronInput, "bad rule"));
  await until(() => text().includes("expected five fields"));
  expect(button("Create schedule").disabled).toBe(true);
});

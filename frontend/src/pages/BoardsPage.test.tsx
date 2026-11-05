// @vitest-environment jsdom
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import BoardsPage from "./BoardsPage";
import type { BoardCard, BoardCardUpdate, BusEvent } from "../api/types";

const fake = vi.hoisted(() => ({ api: {} as Record<string, (...args: any[]) => any>, event: (_e: BusEvent) => {}, reconnect: () => {} }));
vi.mock("../api/client", () => ({ Api: fake.api, getActorHandle: () => "you" }));
vi.mock("../api/sse", () => ({ useBusEvents: (_: unknown, event: typeof fake.event, reconnect: typeof fake.reconnect) => { fake.event = event; fake.reconnect = reconnect; } }));

let root: Root;
let el: HTMLDivElement;
let qc: QueryClient;
let card: BoardCard;
const text = () => el.textContent ?? "";
async function until(condition: () => boolean) {
  const deadline = Date.now() + 2000;
  while (!condition()) {
    if (Date.now() > deadline) throw new Error(`Timed out: ${text()}`);
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)); });
  }
}
function button(label: string) {
  const found = [...el.querySelectorAll("button")].find(b => b.textContent === label);
  if (!found) throw new Error(`Missing button ${label}`);
  return found;
}
async function render() {
  await act(async () => root.render(<QueryClientProvider client={qc}><MemoryRouter><BoardsPage /></MemoryRouter></QueryClientProvider>));
  await until(() => text().includes("Check release"));
}
beforeEach(() => {
  card = { id: "card", board_id: "board", title: "Check release", description: "Review changes", status: "ready",
    assignee_id: null, thread_id: "thread", result: "", blocked_reason: "", version: 2, created_at: "2026-11-05T12:00:00Z", updated_at: "2026-11-05T12:00:00Z" };
  fake.api.listBoards = vi.fn(async () => [{ id: "board", name: "Release", description: "" }]);
  fake.api.listCards = vi.fn(async () => [card]);
  fake.api.listActors = vi.fn(async () => [{ id: "human", handle: "you", name: "You", enabled: true }]);
  fake.api.listThreads = vi.fn(async () => [{ id: "thread", title: "Release discussion" }]);
  fake.api.cardHistory = vi.fn(async () => []);
  fake.api.getCard = vi.fn(async () => card);
  fake.api.claimCard = vi.fn(async () => { card = { ...card, assignee_id: "human", status: "doing", version: 3 }; return card; });
  fake.api.updateCard = vi.fn(async (_id: string, changes: BoardCardUpdate) => { card = { ...card, ...changes, version: card.version + 1 }; return card; });
  el = document.createElement("div"); document.body.append(el); root = createRoot(el);
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
});
afterEach(() => { act(() => root.unmount()); qc.clear(); el.remove(); });

it("claims a ready card and shows its discussion and owner", async () => {
  await render();
  expect(el.querySelector('a[href="/threads/thread"]')).not.toBeNull();
  await act(async () => button("Claim").click());
  await until(() => el.querySelector('section[aria-label="Doing"]')?.textContent?.includes("Check release") ?? false);
  expect(fake.api.claimCard).toHaveBeenCalledWith("card", expect.anything());
  expect(el.querySelector('section[aria-label="Doing"]')?.textContent).toContain("You");
});

it("keeps the edited version after remote updates and lets the user explicitly reload a conflict", async () => {
  await render();
  await act(async () => button("Check release").click());
  card = { ...card, title: "Updated elsewhere", version: 3 };
  await act(async () => fake.event({ event: "board.updated", thread_id: null, data: { board_id: "board" } }));
  await until(() => text().includes("Updated elsewhere"));
  expect((el.querySelector('input') as HTMLInputElement).value).toBe("Check release");
  fake.api.updateCard = vi.fn(async () => { throw new Error("card changed; reload it before retrying"); });
  await act(async () => button("Save card").click());
  await until(() => text().includes("card changed"));
  expect(fake.api.updateCard.mock.calls[0][1].expected_version).toBe(2);
  await act(async () => button("Reload card (discard edits)").click());
  await until(() => (el.querySelector('input') as HTMLInputElement).value === "Updated elsewhere");
});

it("refreshes cards after reconnecting to the event stream", async () => {
  await render();
  card = { ...card, status: "blocked", blocked_reason: "Missing document", version: 3 };
  await act(async () => fake.reconnect());
  await until(() => el.querySelector('section[aria-label="Blocked"]')?.textContent?.includes("Missing document") ?? false);
});

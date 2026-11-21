// @vitest-environment jsdom
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createRoot } from "react-dom/client";
import { act } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import RunCard from "./RunCard";
import { Api } from "../api/client";
import type { Run, RunEvent } from "../api/types";

vi.mock("../api/client", () => ({ Api: { getBotScratchpad: vi.fn() } }));

const run: Run = {
  id: "r", actor_id: "b1", thread_id: "t", status: "completed", interrupt: null, error: null,
  langsmith_run_id: null, created_at: "2026-01-01T00:00:00Z", started_at: null, finished_at: null,
};
const runWith = (actor_id: string): Run => ({ ...run, actor_id });
const ev = (seq: number, type: string, payload: Record<string, unknown>): RunEvent =>
  ({ id: `e${seq}`, run_id: "r", seq, type, payload, created_at: "2026-01-01T00:00:00Z" });

const render = (events: RunEvent[], r: Run = run) => {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  const qc = new QueryClient();
  act(() => root.render(<QueryClientProvider client={qc}><RunCard run={r} events={events} botName="Engineer" /></QueryClientProvider>));
  return { container, cleanup: () => { act(() => root.unmount()); container.remove(); } };
};

describe("RunCard tool failures", () => {
  beforeEach(() => { vi.mocked(Api.getBotScratchpad).mockResolvedValue({ content: "", updated_at: null }); });

  it("counts failed tool calls in the header and marks each one", () => {
    const { container, cleanup } = render([
      ev(1, "tool_call", { id: "a", name: "read_file", args: {} }),
      ev(2, "tool_result", { tool_call_id: "a", status: "success", content: "ok" }),
      ev(3, "tool_call", { id: "b", name: "Linear__save_comment", args: {} }),
      ev(4, "tool_result", { tool_call_id: "b", status: "error", content: "error: bad" }),
    ]);
    expect(container.textContent).toContain("2 tool calls · 1 failed");
    act(() => container.querySelector<HTMLButtonElement>("button")!.click());
    expect([...container.querySelectorAll("[title]")].map((e) => e.getAttribute("title"))).toEqual(["returned", "failed"]);
    cleanup();
  });

  it("says nothing about failures when every call succeeded", () => {
    const { container, cleanup } = render([
      ev(1, "tool_call", { id: "a", name: "read_file", args: {} }),
      ev(2, "tool_result", { tool_call_id: "a", status: "success", content: "ok" }),
    ]);
    expect(container.textContent).not.toContain("failed");
    cleanup();
  });
});

describe("RunCard scratchpad", () => {
  beforeEach(() => { vi.mocked(Api.getBotScratchpad).mockReset(); });

  /** Let the scratchpad query (or its failure) settle. */
  const settle = () => act(async () => {
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
  });
  const padButton = (container: HTMLElement) => container.querySelector<HTMLButtonElement>('button[aria-label="Show scratchpad"]');

  it("shows an icon when the bot has a saved pad, and opens it in a dialog", async () => {
    vi.mocked(Api.getBotScratchpad).mockResolvedValue({ content: "State\n- step one done", updated_at: "2026-01-02T03:04:05Z" });
    const { container, cleanup } = render([], runWith("b2"));
    await settle();
    expect(Api.getBotScratchpad).toHaveBeenCalledWith("b2");
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(container.textContent).not.toContain("step one done");
    await act(async () => { padButton(container)!.click(); });
    await settle();
    const dialog = document.querySelector('[role="dialog"]')!;
    expect(dialog.textContent).toContain("Engineer's scratchpad");
    expect(dialog.textContent).toContain("step one done");
    expect(Api.getBotScratchpad).toHaveBeenCalledTimes(2); // opening refetches the latest write
    act(() => dialog.querySelector<HTMLButtonElement>('button[aria-label="Close"]')!.click());
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    cleanup();
  });

  it("shows no icon when the pad is empty or blank", async () => {
    vi.mocked(Api.getBotScratchpad).mockResolvedValue({ content: "  \n", updated_at: null });
    const { container, cleanup } = render([], runWith("b3"));
    await settle();
    expect(padButton(container)).toBeNull();
    cleanup();
  });

  it("shows no icon (and no crash) when the fetch fails", async () => {
    vi.mocked(Api.getBotScratchpad).mockRejectedValue(new Error("boom"));
    const { container, cleanup } = render([], runWith("b4"));
    await settle();
    expect(padButton(container)).toBeNull();
    expect(container.textContent).not.toContain("boom");
    cleanup();
  });

  it("refetches when a scratchpad save lands, so the icon appears mid-run", async () => {
    vi.mocked(Api.getBotScratchpad).mockResolvedValueOnce({ content: "", updated_at: null });
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    const qc = new QueryClient();
    const view = (events: RunEvent[]) => <QueryClientProvider client={qc}><RunCard run={runWith("b5")} events={events} /></QueryClientProvider>;
    const call = ev(1, "tool_call", { id: "s", name: "scratchpad", args: { action: "write", content: "x" } });
    act(() => root.render(view([call])));
    await settle();
    expect(padButton(container)).toBeNull();
    vi.mocked(Api.getBotScratchpad).mockResolvedValue({ content: "x", updated_at: "2026-01-02T03:04:05Z" });
    act(() => root.render(view([call, ev(2, "tool_result", { tool_call_id: "s", name: "scratchpad", status: "success", content: "Scratchpad replaced (1 characters)." })])));
    await settle();
    expect(Api.getBotScratchpad).toHaveBeenCalledTimes(2);
    expect(padButton(container)).not.toBeNull();
    act(() => root.unmount());
    container.remove();
  });
});

describe("RunCard tool views", () => {
  beforeEach(() => { vi.mocked(Api.getBotScratchpad).mockResolvedValue({ content: "", updated_at: null }); });
  const open = (container: HTMLElement) => {
    act(() => (container.querySelector("button[aria-expanded]") as HTMLButtonElement).click());
    container.querySelectorAll("details").forEach(d => { d.open = true; });
  };
  const summaries = (container: HTMLElement) => [...container.querySelectorAll("summary")].map(s => s.textContent?.trim());
  async function until(condition: () => boolean) {
    const deadline = Date.now() + 3000;
    while (!condition()) {
      if (Date.now() > deadline) throw new Error("timed out");
      await act(async () => { await new Promise(r => setTimeout(r, 20)); });
    }
  }

  it("reads shell calls of every agent as commands", () => {
    const { container, cleanup } = render([
      ev(1, "tool_call", { id: "p", name: "bash", args: { command: "ls -la" } }),
      ev(2, "tool_result", { tool_call_id: "p", status: "success", content: "\x1b[32mtotal 0\x1b[0m" }),
      ev(3, "tool_call", { id: "c", name: "Bash", args: { command: "git status\ngit diff", description: "check" } }),
    ]);
    open(container);
    expect(summaries(container)).toEqual(["$ls -la", "$git status …"]);
    expect(container.textContent).toContain("total 0");
    expect(container.textContent).not.toContain("\x1b[");
    cleanup();
  });

  it("names reads by path and range, without the line-number prefixes", () => {
    const { container, cleanup } = render([
      ev(1, "tool_call", { id: "r", name: "Read", args: { file_path: "src/a.py", offset: 2, limit: 3 } }),
      ev(2, "tool_result", { tool_call_id: "r", status: "success", content: "     2\tx = 1\n     3\ty = 2" }),
    ]);
    open(container);
    expect(summaries(container)).toEqual(["$read src/a.py:2-4"]);
    expect(container.textContent).toContain("x = 1\ny = 2");
    cleanup();
  });

  it("shows unknown tools by name with their arguments", () => {
    const { container, cleanup } = render([ev(1, "tool_call", { id: "m", name: "mcp__linear__save", args: { id: 7 } })]);
    open(container);
    expect(summaries(container)).toEqual(['$mcp__linear__save({"id":7})']);
    expect(container.textContent).toContain('"id": 7');
    cleanup();
  });

  it("shows an edit with its patch as a numbered diff and counts the lines", async () => {
    const patch = "--- a.py\n+++ a.py\n@@ -1,2 +1,2 @@\n x = 1\n-y = 2\n+y = 3\n";
    const { container, cleanup } = render([
      ev(1, "tool_call", { id: "e", name: "edit", args: { path: "a.py", edits: [{ oldText: "y = 2", newText: "y = 3" }] } }),
      ev(2, "tool_result", { tool_call_id: "e", status: "success", content: "Successfully replaced 1 block(s)", patch }),
    ]);
    open(container);
    expect(summaries(container)).toEqual(["$edit a.py +1 −1"]);
    await until(() => !!container.querySelector('[data-testid="edit-diff"]'));
    const diff = container.querySelector('[data-testid="edit-diff"]')!;
    expect(diff.textContent).toContain("@@ -1,2 +1,2 @@");
    expect(diff.querySelectorAll(".bg-danger\\/10, .bg-ok\\/10")).toHaveLength(2);
    cleanup();
  });

  it("falls back to the fragments without a patch, and shows errors first", async () => {
    const { container, cleanup } = render([
      ev(1, "tool_call", { id: "e", name: "edit_file", args: { path: "a.py", old_string: "y = 2", new_string: "y = 3" } }),
      ev(2, "tool_result", { tool_call_id: "e", status: "error", content: "error: old_string not found" }),
    ]);
    open(container);
    await until(() => !!container.querySelector('[data-testid="edit-diff"]'));
    expect(container.textContent).toContain("no line numbers");
    const body = container.querySelector("details > div")!;
    expect(body.firstElementChild?.textContent).toBe("error: old_string not found");
    cleanup();
  });
});

// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from "vitest";
import { DRAFT_TTL, deleteSavedDrafts, readDraft, setSavesDrafts, storeDraft } from "./draftStorage";
beforeEach(() => { localStorage.clear(); vi.resetModules(); });
it("restores text after module/page reload, never sends it, and flags uncertain sends", async () => {
  let drafts = await import("./threadDrafts");
  drafts.setThreadDraft("t", "sensitive draft");
  drafts.beginThreadSend("t");
  vi.resetModules();
  drafts = await import("./threadDrafts");
  expect(drafts.getThreadDraft("t")).toBe("sensitive draft");
  expect(drafts.hasUncertainSend("t")).toBe(true);
  const rev = drafts.beginThreadSend("t");
  drafts.endThreadSend("t", rev, true);
  expect(readDraft("t")).toBeUndefined();
});
it("does not delete newer edits when an older send succeeds", async () => {
  const drafts = await import("./threadDrafts");
  drafts.setThreadDraft("t", "first");
  const rev = drafts.beginThreadSend("t");
  drafts.setThreadDraft("t", "second");
  expect(drafts.endThreadSend("t", rev, true)).toBe(false);
  expect(readDraft("t")?.text).toBe("second");
  expect(readDraft("t")?.uncertain).toBe(false);
});
it("expires, bounds and validates stored drafts without touching other local data", () => {
  localStorage.setItem("unrelated", "keep");
  localStorage.setItem("openbot:draft:v1:old", JSON.stringify({ text: "old", updated: Date.now() - DRAFT_TTL - 1 }));
  localStorage.setItem("openbot:draft:v1:bad", "{");
  expect(readDraft("old")).toBeUndefined();
  for (let i = 0; i < 25; i++) storeDraft(String(i), "text");
  expect(Object.keys(localStorage).filter((k) => k.startsWith("openbot:draft:"))).toHaveLength(20);
  storeDraft("large", "x".repeat(64_001));
  expect(readDraft("large")).toBeUndefined();
  deleteSavedDrafts();
  expect(localStorage.getItem("unrelated")).toBe("keep");
});
it("can disable persistence while keeping the in-memory composer working", async () => {
  storeDraft("t", "old");
  setSavesDrafts(false);
  const drafts = await import("./threadDrafts");
  drafts.setThreadDraft("t", "memory only");
  expect(drafts.getThreadDraft("t")).toBe("memory only");
  expect(readDraft("t")).toBeUndefined();
});
it("survives unavailable storage", async () => {
  const spy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("quota"); });
  try {
    const drafts = await import("./threadDrafts");
    drafts.setThreadDraft("t", "not lost");
    expect(drafts.getThreadDraft("t")).toBe("not lost");
  } finally { spy.mockRestore(); }
});

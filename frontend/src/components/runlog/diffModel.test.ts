import { describe, expect, it } from "vitest";
import { fromEdits, fromPatch, pieces, sides, type Line } from "./diffModel";

const PATCH = [
  "--- a/src/greet.py", "+++ b/src/greet.py",
  "@@ -1,3 +1,3 @@", " def greet(name):", '-    return "hi " + name', '+    return "hello " + name', " ",
  "@@ -10,2 +10,3 @@", " x = 1", "+y = 2", " z = 3",
].join("\n") + "\n";

const lines = (rows: ReturnType<typeof fromEdits>[number]["rows"]) => rows.filter((r): r is Line => r.kind === "line");

describe("fromPatch", () => {
  it("numbers both sides per hunk and names the file", () => {
    const [file] = fromPatch(PATCH)!;
    expect(file.name).toBe("src/greet.py");
    expect(file.rows.filter(r => r.kind === "hunk").map(r => r.kind === "hunk" && r.header)).toEqual(["@@ -1,3 +1,3 @@", "@@ -10,2 +10,3 @@"]);
    expect(lines(file.rows).map(l => [l.sign, l.oldNo, l.newNo])).toEqual([
      [" ", 1, 1], ["-", 2, undefined], ["+", undefined, 2], [" ", 3, 3],
      [" ", 10, 10], ["+", undefined, 11], [" ", 11, 12],
    ]);
  });

  it("marks only the changed word of a replaced line", () => {
    const [file] = fromPatch(PATCH)!;
    const [removed, added] = lines(file.rows).filter(l => l.sign !== " ");
    expect(removed.changed!.map(([a, b]) => removed.text.slice(a, b))).toEqual(['hi']);
    expect(added.changed!.map(([a, b]) => added.text.slice(a, b))).toEqual(['hello']);
  });

  it("does not mark words when most of the line changed", () => {
    const [file] = fromPatch("--- a\n+++ a\n@@ -1 +1 @@\n-alpha beta gamma\n+one two three\n")!;
    expect(lines(file.rows).every(l => l.changed === undefined)).toBe(true);
  });

  it("keeps the no-newline marker and reads several files", () => {
    const files = fromPatch("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b\n--- /dev/null\n+++ b/new\n@@ -0,0 +1 @@\n+n\n")!;
    expect(files.map(f => f.name)).toEqual(["x", "new"]);
    expect(files[0].rows.some(r => r.kind === "note" && r.text === "No newline at end of file")).toBe(true);
  });

  it("returns null without hunks", () => {
    expect(fromPatch("just text")).toBeNull();
    expect(fromPatch("")).toBeNull();
  });
});

describe("fromEdits", () => {
  it("shows whole fragments without line numbers", () => {
    const [file] = fromEdits([{ old: "a = 1\nb = 2", new: "a = 1\nb = 3\nc = 4" }]);
    expect(file.numbered).toBe(false);
    expect(file.rows.some(r => r.kind === "hunk" || r.kind === "note")).toBe(false);
    expect(lines(file.rows).map(l => `${l.sign}${l.text}`)).toEqual([" a = 1", "-b = 2", "+b = 3", "+c = 4"]);
    expect(lines(file.rows).every(l => l.oldNo === undefined && l.newNo === undefined)).toBe(true);
  });

  it("handles a deletion and one file per edit", () => {
    const files = fromEdits([{ old: "x", new: "" }, { old: "", new: "y" }]);
    expect(files).toHaveLength(2);
    expect(lines(files[0].rows).map(l => l.sign)).toEqual(["-", "+"]);
  });
});

it("splits each side into text for one highlighting pass", () => {
  const [file] = fromPatch(PATCH)!;
  const { oldText, newText, at } = sides(file.rows);
  expect(oldText.split("\n")).toEqual(["def greet(name):", '    return "hi " + name', "", "x = 1", "z = 3"]);
  expect(newText.split("\n")).toEqual(["def greet(name):", '    return "hello " + name', "", "x = 1", "y = 2", "z = 3"]);
  expect(at[0]).toBeNull();  // hunk header
  expect(at[3]).toEqual({ side: "new", index: 1 });
});

describe("pieces", () => {
  const tokens = [{ content: "return ", offset: 0, htmlStyle: { "--shiki-light": "#a" } }, { content: '"hello"', offset: 7, htmlStyle: { "--shiki-light": "#b" } }];
  it("cuts tokens at changed ranges and keeps their colours", () => {
    expect(pieces('return "hello"', tokens, [[8, 13]])).toEqual([
      { text: "return ", style: { "--shiki-light": "#a" }, mark: false },
      { text: '"', style: { "--shiki-light": "#b" }, mark: false },
      { text: "hello", style: { "--shiki-light": "#b" }, mark: true },
      { text: '"', style: { "--shiki-light": "#b" }, mark: false },
    ]);
  });
  it("falls back to plain text when the tokens do not match the line", () => {
    expect(pieces("other", tokens, undefined)).toEqual([{ text: "other", style: undefined, mark: false }]);
  });
});

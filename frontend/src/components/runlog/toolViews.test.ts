import { describe, expect, it } from "vitest";
import { argsPreview, patchStats, stripAnsi, stripLineNumbers, toolView } from "./toolViews";

describe("toolView", () => {
  it.each([
    // shell
    ["bash", { command: "ls -la", timeout: 10 }, { kind: "shell", command: "ls -la" }],  // Pi
    ["Bash", { command: "ls -la", description: "list" }, { kind: "shell", command: "ls -la" }],  // Claude
    ["command_execution", { command: "ls", aggregated_output: "a", exit_code: 0 }, { kind: "shell", command: "ls" }],  // Codex
    ["run_shell", { command: "make", cwd: "backend" }, { kind: "shell", command: "make", cwd: "backend" }],  // native
    // read
    ["read", { path: "a.py", offset: 10, limit: 5 }, { kind: "read", path: "a.py", range: "10-14" }],
    ["Read", { file_path: "a.py" }, { kind: "read", path: "a.py" }],
    ["read_file", { path: "a.py", start_line: 3, end_line: 9 }, { kind: "read", path: "a.py", range: "3-9" }],
    // write
    ["write", { path: "n.md", content: "# x" }, { kind: "write", path: "n.md", content: "# x" }],
    ["Write", { file_path: "n.md", content: "" }, { kind: "write", path: "n.md", content: "" }],
    // edit
    ["edit", { path: "a.py", edits: [{ oldText: "a", newText: "b" }] }, { kind: "edit", path: "a.py", edits: [{ old: "a", new: "b" }] }],
    ["edit", { path: "a.py", oldText: "a", newText: "b" }, { kind: "edit", path: "a.py", edits: [{ old: "a", new: "b" }] }],  // older Pi
    ["Edit", { file_path: "a.py", old_string: "a", new_string: "b", replace_all: true }, { kind: "edit", path: "a.py", edits: [{ old: "a", new: "b" }] }],
    ["MultiEdit", { file_path: "a.py", edits: [{ old_string: "a", new_string: "b" }, { old_string: "c", new_string: "" }] },
      { kind: "edit", path: "a.py", edits: [{ old: "a", new: "b" }, { old: "c", new: "" }] }],
    ["edit_file", { path: "a.py", old_string: "a", new_string: "b" }, { kind: "edit", path: "a.py", edits: [{ old: "a", new: "b" }] }],
    ["patch_file", { diff_input: "--- a\n+++ a\n" }, { kind: "edit", path: "", edits: [], patch: "--- a\n+++ a\n" }],
    // search and listing
    ["grep", { pattern: "TODO", path: "src" }, { kind: "search", tool: "grep", pattern: "TODO", where: "src" }],
    ["Glob", { pattern: "**/*.ts" }, { kind: "search", tool: "glob", pattern: "**/*.ts" }],
    ["search_code", { pattern: "foo", glob: "*.py" }, { kind: "search", tool: "search_code", pattern: "foo", where: "*.py" }],
    ["find_files", { glob_pattern: "*.nix" }, { kind: "search", tool: "find_files", pattern: "*.nix" }],
    ["ls", { path: "src" }, { kind: "search", tool: "ls", pattern: "src" }],
    ["list_files", {}, { kind: "search", tool: "list_files", pattern: "." }],
    // Codex file changes
    ["file_change", { changes: [{ path: "a.py", kind: "update" }, { path: "b.py" }] },
      { kind: "files", changes: [{ path: "a.py", kind: "update" }, { path: "b.py", kind: "change" }] }],
  ])("%s %j", (name, args, view) => {
    expect(toolView(name, args)).toMatchObject(view);
  });

  it.each([
    ["bash", {}], ["bash", { command: 3 }], ["read", { offset: 1 }], ["write", { path: "a" }],
    ["edit", { path: "a", edits: [{ oldText: "a" }] }], ["edit", { path: "a", edits: [] }], ["Edit", { old_string: "a", new_string: "b" }],
    ["file_change", { changes: "a.py" }], ["file_change", { changes: [{ kind: "add" }] }], ["grep", {}],
    ["mcp__linear__save", { id: 1 }], ["bash", null], ["read", "a.py"], ["edit", [1, 2]],
  ])("falls back to generic for %s %j", (name, args) => {
    expect(toolView(name, args)).toEqual({ kind: "generic" });
  });
});

it("counts patch lines without headers", () => {
  expect(patchStats("--- a.py\n+++ a.py\n@@ -1,2 +1,3 @@\n a\n-b\n+B\n+C\n")).toEqual({ added: 2, removed: 1 });
});

it("strips the line numbers of all three read formats", () => {
  expect(stripLineNumbers("     1\tdef f():\n     2\t    pass\n")).toBe("def f():\n    pass\n");  // Claude 2.1.3xx
  expect(stripLineNumbers("1→a\n2→b")).toBe("a\nb");  // older Claude
  expect(stripLineNumbers("1: a\n2: b\n[showing lines 1-2 of 9; continue at 3]")).toBe("a\nb\n[showing lines 1-2 of 9; continue at 3]");  // native
  expect(stripLineNumbers("plain\n2: not a prefix on line one")).toBe("plain\n2: not a prefix on line one");
});

it("strips ANSI colours and titles", () => {
  expect(stripAnsi("\x1b[1;32mok\x1b[0m \x1b]0;title\x07done")).toBe("ok done");
});

it("previews arguments on one line", () => {
  expect(argsPreview({})).toBe("");
  expect(argsPreview({ a: 1 })).toBe('{"a":1}');
  expect(argsPreview({ text: "x".repeat(200) })).toHaveLength(120);
});

/* What a tool call means, independent of which agent made it.
 *
 * Pi, Claude Code, Codex and native bots name and shape the same operations differently (see
 * docs/highlighting_plan.md, "Tool shapes"). `toolView` maps a call onto one of a few views the
 * run log knows how to show; anything unexpected becomes `generic`. It never throws: plugins and
 * MCP servers send arbitrary arguments. */

export type Edit = { old: string; new: string };
export type ToolView =
  | { kind: "shell"; command: string; cwd?: string }
  | { kind: "read"; path: string; range?: string }
  | { kind: "write"; path: string; content: string }
  | { kind: "edit"; path: string; edits: Edit[]; patch?: string }
  | { kind: "search"; tool: string; pattern: string; where?: string }
  | { kind: "files"; changes: { path: string; kind: string }[] }
  | { kind: "generic" };

type Args = Record<string, unknown>;
const str = (v: unknown): string | undefined => (typeof v === "string" ? v : undefined);
const num = (v: unknown): number | undefined => (typeof v === "number" && Number.isFinite(v) ? v : undefined);
const first = (args: Args, ...keys: string[]) => keys.map(k => str(args[k])).find(v => v !== undefined);

function range(args: Args): string | undefined {
  const start = num(args.start_line);  // native read_file: inclusive 1-based lines
  const end = num(args.end_line);
  if (start !== undefined || end !== undefined) return `${start ?? 1}-${end ?? ""}`;
  const offset = num(args.offset);  // Pi and Claude: first line (1-based) and a count
  const limit = num(args.limit);
  if (offset === undefined && limit === undefined) return undefined;
  const from = offset ?? 1;
  return limit !== undefined ? `${from}-${from + limit - 1}` : `${from}-`;
}

function edits(args: Args): Edit[] | null {
  const pair = (o: Args): Edit | null => {
    const old = first(o, "old_string", "oldText"), neu = first(o, "new_string", "newText");
    return old !== undefined && neu !== undefined ? { old, new: neu } : null;
  };
  if (Array.isArray(args.edits)) {
    const list = args.edits.map(e => (e && typeof e === "object" ? pair(e as Args) : null));
    return list.length > 0 && list.every(Boolean) ? (list as Edit[]) : null;
  }
  const single = pair(args);
  return single ? [single] : null;
}

/** Normalize one call. `name` is the tool name as the agent reported it. */
export function toolView(name: string, rawArgs: unknown): ToolView {
  const args: Args = rawArgs && typeof rawArgs === "object" && !Array.isArray(rawArgs) ? (rawArgs as Args) : {};
  const tool = name.toLowerCase();
  const path = first(args, "path", "file_path", "filePath");
  switch (tool) {
    case "bash": case "run_shell": case "command_execution": case "shell": {
      const command = str(args.command) ?? (Array.isArray(args.command) && args.command.every(p => typeof p === "string") ? args.command.join(" ") : undefined);
      return command !== undefined ? { kind: "shell", command, cwd: str(args.cwd) } : { kind: "generic" };
    }
    case "read": case "read_file":
      return path ? { kind: "read", path, range: range(args) } : { kind: "generic" };
    case "write": case "write_file": {
      const content = str(args.content);
      return path && content !== undefined ? { kind: "write", path, content } : { kind: "generic" };
    }
    case "edit": case "edit_file": case "multiedit": {
      const list = edits(args);
      return path && list ? { kind: "edit", path, edits: list } : { kind: "generic" };
    }
    case "patch_file": {
      const patch = str(args.diff_input);
      return patch ? { kind: "edit", path: path ?? "", edits: [], patch } : { kind: "generic" };
    }
    case "grep": case "glob": case "find": case "ls": case "search_code": case "find_files": case "list_files": {
      const pattern = first(args, "pattern", "glob_pattern", "query") ?? (tool === "ls" || tool === "list_files" ? path ?? "." : undefined);
      if (pattern === undefined) return { kind: "generic" };
      const where = tool === "ls" || tool === "list_files" ? undefined : first(args, "path", "glob", "include");
      return { kind: "search", tool, pattern, where };
    }
    case "file_change": {
      const changes = Array.isArray(args.changes) ? args.changes : null;
      const ok = changes?.every(c => c && typeof c === "object" && typeof (c as Args).path === "string");
      return changes && ok && changes.length > 0
        ? { kind: "files", changes: changes.map(c => ({ path: (c as Args).path as string, kind: str((c as Args).kind) ?? "change" })) }
        : { kind: "generic" };
    }
    default:
      return { kind: "generic" };
  }
}

/** Added and removed lines in a unified patch (headers excluded). */
export function patchStats(patch: string): { added: number; removed: number } {
  let added = 0, removed = 0;
  for (const line of patch.split("\n")) {
    if (line.startsWith("+++ ") || line.startsWith("--- ")) continue;
    if (line.startsWith("+")) added += 1;
    else if (line.startsWith("-")) removed += 1;
  }
  return { added, removed };
}

// Read results number their lines: Claude `   12\t`, older Claude `12→`, native read_file `12: `.
const NUMBERED = /^\s*\d+(?:\t|→|: )/;

/** File text without the line-number prefixes read tools add, when the first line has one. */
export function stripLineNumbers(text: string): string {
  const lines = text.split("\n");
  if (!NUMBERED.test(lines[0] ?? "")) return text;
  return lines.map(line => line.replace(NUMBERED, "")).join("\n");
}

// eslint-disable-next-line no-control-regex
const ANSI = /\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g;
export const stripAnsi = (text: string) => text.replace(ANSI, "");

/** A one-line preview of arbitrary args for the generic summary. */
export function argsPreview(args: unknown, max = 120): string {
  let text: string;
  try { text = JSON.stringify(args) ?? ""; } catch { text = String(args); }
  if (text === "{}" || text === "null" || text === "") return "";
  return text.length > max ? text.slice(0, max - 1) + "…" : text;
}

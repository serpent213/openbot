/* Rows for the run log's edit diff: parsed from a unified patch (real line numbers) or computed
 * from an edit's old/new fragments (no line numbers: they would be relative to the fragment).
 * Changed words within a replaced line are marked as character ranges. Pure, for tests. */
import type { CSSProperties } from "react";
import type { ThemedToken } from "shiki/core";
import { diffWordsWithSpace, parsePatch, structuredPatch } from "diff";
import type { Edit } from "./toolViews";

export type Range = [start: number, end: number];
export type Line = { kind: "line"; sign: " " | "+" | "-"; text: string; oldNo?: number; newNo?: number; changed?: Range[] };
export type Row = { kind: "hunk"; header: string } | Line | { kind: "note"; text: string };
export type DiffFile = { name?: string; numbered: boolean; rows: Row[] };

type Hunk = { oldStart: number; oldLines: number; newStart: number; newLines: number; lines: string[] };

// Marking words only helps while most of the line stays; past this share it is noise.
const MAX_CHANGED_SHARE = 0.6;

function wordRanges(oldText: string, newText: string): [Range[], Range[]] | null {
  const parts = diffWordsWithSpace(oldText, newText);
  const removed: Range[] = [], added: Range[] = [];
  let o = 0, n = 0, changed = 0;
  for (const part of parts) {
    const length = part.value.length;
    if (part.removed) { removed.push([o, o + length]); o += length; changed += length; }
    else if (part.added) { added.push([n, n + length]); n += length; changed += length; }
    else { o += length; n += length; }
  }
  const total = oldText.length + newText.length;
  if (!total || changed / total > MAX_CHANGED_SHARE) return null;
  return [removed, added];
}

/** Pair each run of removed lines with the added lines after it, line by line, and mark words. */
function markWords(rows: Row[]): void {
  for (let i = 0; i < rows.length;) {
    const removed: Line[] = [];
    while (i < rows.length && rows[i].kind === "line" && (rows[i] as Line).sign === "-") removed.push(rows[i++] as Line);
    const added: Line[] = [];
    while (i < rows.length && rows[i].kind === "line" && (rows[i] as Line).sign === "+") added.push(rows[i++] as Line);
    if (removed.length === 0 && added.length === 0) { i += 1; continue; }
    for (let k = 0; k < Math.min(removed.length, added.length); k += 1) {
      const ranges = wordRanges(removed[k].text, added[k].text);
      if (ranges) [removed[k].changed, added[k].changed] = ranges;
    }
  }
}

function rowsFromHunks(hunks: Hunk[], numbered: boolean): Row[] {
  const rows: Row[] = [];
  for (const hunk of hunks) {
    if (numbered) rows.push({ kind: "hunk", header: `@@ -${hunk.oldStart},${hunk.oldLines} +${hunk.newStart},${hunk.newLines} @@` });
    let oldNo = hunk.oldStart, newNo = hunk.newStart;
    for (const raw of hunk.lines) {
      const sign = raw[0];
      const text = raw.slice(1);
      if (sign === "\\") { rows.push({ kind: "note", text: raw.slice(2) }); continue; }
      if (sign === "+") rows.push({ kind: "line", sign, text, ...(numbered ? { newNo: newNo++ } : {}) });
      else if (sign === "-") rows.push({ kind: "line", sign, text, ...(numbered ? { oldNo: oldNo++ } : {}) });
      else rows.push({ kind: "line", sign: " ", text, ...(numbered ? { oldNo: oldNo++, newNo: newNo++ } : {}) });
    }
  }
  markWords(rows);
  return rows;
}

const stripPrefix = (name: string | undefined) =>
  name && name !== "/dev/null" ? name.replace(/^[ab]\//, "") : undefined;

/** Files and rows of a unified patch; null when it has no hunks we can read. */
export function fromPatch(patch: string): DiffFile[] | null {
  let parsed;
  try { parsed = parsePatch(patch); } catch { return null; }
  const files = parsed.filter(f => f.hunks.length > 0).map(f => ({
    name: stripPrefix(f.newFileName) ?? stripPrefix(f.oldFileName),
    numbered: true,
    rows: rowsFromHunks(f.hunks, true),
  }));
  return files.length > 0 ? files : null;
}

/** One unnumbered diff per edit fragment, with every line of both fragments shown. */
export function fromEdits(edits: Edit[]): DiffFile[] {
  return edits.map(edit => {
    // A missing final newline on both sides is a fragment boundary, not a change worth a note.
    const tidy = (s: string) => (s.endsWith("\n") ? s : s + "\n");
    const patch = structuredPatch("a", "a", tidy(edit.old), tidy(edit.new), undefined, undefined, { context: Number.MAX_SAFE_INTEGER });
    return { numbered: false, rows: rowsFromHunks(patch.hunks, false) };
  });
}

/** The old and new side of each hunk as text, for highlighting a side in one pass so tokens that
 *  span lines (strings, comments) come out right. Returns per row the side and line index. */
export function sides(rows: Row[]): { oldText: string; newText: string; at: ({ side: "old" | "new"; index: number } | null)[] } {
  const oldLines: string[] = [], newLines: string[] = [];
  const at = rows.map(row => {
    if (row.kind !== "line") return null;
    if (row.sign === "+") { newLines.push(row.text); return { side: "new" as const, index: newLines.length - 1 }; }
    oldLines.push(row.text);
    if (row.sign === " ") newLines.push(row.text);
    return { side: "old" as const, index: oldLines.length - 1 };
  });
  return { oldText: oldLines.join("\n"), newText: newLines.join("\n"), at };
}

export type Piece = { text: string; style?: CSSProperties; mark: boolean };

/** Split a line's tokens at the changed-word boundaries. */
export function pieces(text: string, tokens: ThemedToken[] | undefined, changed: Range[] | undefined): Piece[] {
  const usable = tokens && tokens.map(t => t.content).join("") === text;
  const parts = usable ? tokens!.map(t => ({ text: t.content, style: t.htmlStyle as CSSProperties | undefined })) : [{ text, style: undefined }];
  const marks = changed ?? [];
  const out: Piece[] = [];
  let pos = 0;
  for (const part of parts) {
    const end = pos + part.text.length;
    const cuts = [pos, end, ...marks.flat().filter(c => c > pos && c < end)].sort((a, b) => a - b);
    for (let i = 0; i < cuts.length - 1; i += 1) {
      const [a, b] = [cuts[i], cuts[i + 1]];
      if (a === b) continue;
      out.push({ text: part.text.slice(a - pos, b - pos), style: part.style, mark: marks.some(([x, y]) => a >= x && b <= y) });
    }
    pos = end;
  }
  return out;
}

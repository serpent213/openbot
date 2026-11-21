/* A unified diff for one tool call's edit: real line numbers from a patch, or the old/new
 * fragments without numbers. Lines are syntax highlighted by the file's language (each side of a
 * hunk in one pass) and changed words are marked. Colours come from the theme tokens and the
 * shared Shiki CSS, so light, dark and system all work. Loaded lazily (it pulls in jsdiff). */
import { useMemo, type ReactNode } from "react";
import type { ThemedToken } from "shiki/core";
import { langFromPath, tokenize, useHighlighter } from "../../lib/highlight";
import { fromEdits, fromPatch, pieces, sides, type DiffFile, type Line } from "./diffModel";
import type { Edit } from "./toolViews";

export type EditDiffProps = { patch?: string; edits?: Edit[]; path?: string };

const rowTone = { "+": "bg-ok/10", "-": "bg-danger/10", " ": "" } as const;
const signTone = { "+": "text-ok", "-": "text-danger", " ": "text-faint" } as const;
const markTone = { "+": "rounded-sm bg-ok/25", "-": "rounded-sm bg-danger/25", " ": "" } as const;

function LineRow({ line, numbered, tokens }: { line: Line; numbered: boolean; tokens?: ThemedToken[] }) {
  return <div className={`flex ${rowTone[line.sign]}`}>
    {numbered && <>
      <span className="w-9 shrink-0 select-none pr-1 text-right text-faint">{line.oldNo ?? ""}</span>
      <span className="w-9 shrink-0 select-none pr-1 text-right text-faint">{line.newNo ?? ""}</span>
    </>}
    <span className={`w-4 shrink-0 select-none text-center ${signTone[line.sign]}`} aria-hidden>{line.sign === " " ? "" : line.sign === "-" ? "−" : "+"}</span>
    <span className="shiki min-w-0 flex-1 whitespace-pre-wrap break-all pr-2 text-fg">
      {pieces(line.text, tokens, line.changed).map((p, i) =>
        <span key={i} style={p.style} className={p.mark ? markTone[line.sign] : undefined}>{p.text}</span>)}
    </span>
  </div>;
}

function FileDiff({ file, lang, ready }: { file: DiffFile; lang: string | null; ready: number }) {
  const tokens = useMemo(() => {
    void ready;  // tokenize again once the file's language has loaded
    const { oldText, newText, at } = sides(file.rows);
    const oldTokens = tokenize(oldText, lang), newTokens = tokenize(newText, lang);
    return at.map(pos => (pos ? (pos.side === "old" ? oldTokens : newTokens)?.[pos.index] : undefined));
  }, [file, lang, ready]);
  const rows: ReactNode[] = file.rows.map((row, i) => {
    if (row.kind === "hunk") return <div key={i} className="bg-sunken/70 px-2 text-faint">{row.header}</div>;
    if (row.kind === "note") return <div key={i} className="px-2 italic text-faint">\ {row.text}</div>;
    return <LineRow key={i} line={row} numbered={file.numbered} tokens={tokens[i]} />;
  });
  return <>{rows}</>;
}

export default function EditDiff({ patch, edits, path }: EditDiffProps) {
  const files = useMemo(() => (patch ? fromPatch(patch) : null) ?? (edits?.length ? fromEdits(edits) : null), [patch, edits]);
  const langs = [path, ...(files ?? []).map(f => f.name)].map(langFromPath);
  const ready = useHighlighter(langs);
  if (!files) return <pre className="whitespace-pre-wrap break-words text-muted">{patch ?? ""}</pre>;
  const named = files.length > 1;
  return <div className="font-mono text-[11px] leading-relaxed" data-testid="edit-diff">
    {!files[0].numbered && <p className="px-2 pb-1 font-sans text-faint">Edited fragment{files.length > 1 ? "s" : ""}, no line numbers</p>}
    {files.map((file, i) => <div key={i} className={i > 0 ? "mt-2 border-t border-line pt-1" : ""}>
      {(named || (!file.numbered && files.length > 1)) && <div className="px-2 pb-0.5 text-muted">{file.name ?? `edit ${i + 1}`}</div>}
      <FileDiff file={file} lang={langFromPath(file.name ?? path)} ready={ready} />
    </div>)}
  </div>;
}

/* One tool call in the run log: a summary line that reads like what the tool did (`$ ls -la`,
 * `read src/a.ts:10-40`, `edit src/a.ts +3 −1`) and, folded beneath, the view that suits it:
 * command output, highlighted file contents, an edit diff, or the arguments as JSON. */
import { lazy, Suspense, type ReactNode } from "react";
import type { RunEvent } from "../../api/types";
import { highlight, highlightInline, langFromPath, useHighlighter } from "../../lib/highlight";
import { argsPreview, patchStats, stripAnsi, stripLineNumbers, toolView, type Edit, type ToolView } from "./toolViews";

const EditDiff = lazy(() => import("./EditDiff"));

const box = "max-h-60 overflow-auto rounded-ui border border-line bg-canvas p-2 text-[11px] leading-relaxed scrollbar-subtle";

/** A highlighted block, or plain text until (or unless) the language is available. */
export function CodeView({ code, lang, className = "" }: { code: string; lang: string | null; className?: string }) {
  useHighlighter([lang]);
  const html = lang ? highlight(code, lang) : null;
  if (html) return <div className={`${box} [&_pre]:m-0 [&_pre]:whitespace-pre-wrap [&_pre]:break-words ${className}`} dangerouslySetInnerHTML={{ __html: html }} />;
  return <pre className={`${box} whitespace-pre-wrap break-words text-fg ${className}`}>{code}</pre>;
}

function InlineCode({ code, lang }: { code: string; lang: string }) {
  useHighlighter([lang]);
  const html = highlightInline(code, lang);
  return html !== null
    ? <span className="shiki whitespace-pre-wrap" dangerouslySetInnerHTML={{ __html: html }} />
    : <span className="whitespace-pre-wrap text-fg">{code}</span>;
}

const Verb = ({ children }: { children: ReactNode }) => <span className="font-medium text-fg">{children}</span>;
const Path = ({ children }: { children: ReactNode }) => <span className="break-all text-muted">{children}</span>;

function Stats({ patch }: { patch?: string }) {
  if (!patch) return null;
  const { added, removed } = patchStats(patch);
  return <span className="whitespace-nowrap"> <span className="text-ok">+{added}</span> <span className="text-danger">−{removed}</span></span>;
}

function Summary({ name, args, view, patch }: { name: string; args: unknown; view: ToolView; patch?: string }) {
  switch (view.kind) {
    case "shell": {
      const [line, ...more] = view.command.split("\n");
      return <><InlineCode code={line} lang="shellscript" />{more.length > 0 && <span className="text-faint"> …</span>}</>;
    }
    case "read": return <><Verb>read</Verb> <Path>{view.path}{view.range && `:${view.range}`}</Path></>;
    case "write": return <><Verb>write</Verb> <Path>{view.path}</Path><Stats patch={patch} /></>;
    case "edit": return <><Verb>{name === "patch_file" ? "patch" : "edit"}</Verb> <Path>{view.path}</Path><Stats patch={patch ?? view.patch} /></>;
    case "search": return <><Verb>{view.tool}</Verb> <span className="text-fg">{view.pattern}</span>{view.where && <span className="text-muted"> in {view.where}</span>}</>;
    case "files": return <><Verb>{name}</Verb> <span className="text-muted">{view.changes.length} file{view.changes.length === 1 ? "" : "s"}</span></>;
    default: {
      const preview = argsPreview(args);
      return <><Verb>{name}</Verb>{preview && <span className="whitespace-pre-wrap text-muted">({preview})</span>}</>;
    }
  }
}

function Output({ text }: { text: string }) {
  return <pre className={`${box} whitespace-pre-wrap break-words text-muted`}>{text}</pre>;
}

function Diff({ patch, edits, path, note }: { patch?: string; edits?: Edit[]; path?: string; note?: string }) {
  const fallback = <pre className={`${box} whitespace-pre-wrap break-words text-muted`}>{patch ?? edits?.map(e => `- ${e.old}\n+ ${e.new}`).join("\n\n")}</pre>;
  return <div className={`${box} max-h-80 p-0 py-1`}>
    {note && <p className="px-2 pb-1 font-sans text-faint">{note}</p>}
    <Suspense fallback={fallback}><EditDiff patch={patch} edits={edits} path={path} /></Suspense>
  </div>;
}

function Body({ args, view, result }: { args: unknown; view: ToolView; result?: RunEvent }) {
  const content = result ? String(result.payload.content ?? "") : null;
  const failed = result?.payload.status === "error";
  const patch = typeof result?.payload.patch === "string" ? result.payload.patch : undefined;
  const truncated = result?.payload.patch_truncated === true ? "The patch was too large to keep; showing the edited fragments." : undefined;
  const parts: ReactNode[] = [];
  if (failed) parts.push(<pre key="error" className={`${box} whitespace-pre-wrap break-words border-danger/30 bg-danger/5 text-danger`}>{content}</pre>);
  switch (view.kind) {
    case "shell":
      if (view.command.includes("\n")) parts.push(<CodeView key="command" code={view.command} lang="shellscript" />);
      if (!failed) parts.push(<Output key="out" text={content === null ? "…" : stripAnsi(content) || "(no output)"} />);
      break;
    case "read":
      if (!failed) parts.push(content === null ? <Output key="out" text="…" /> : <CodeView key="out" code={stripLineNumbers(content)} lang={langFromPath(view.path)} />);
      break;
    case "write":
      parts.push(patch ? <Diff key="diff" patch={patch} path={view.path} /> : <CodeView key="content" code={view.content} lang={langFromPath(view.path)} />);
      break;
    case "edit": {
      // The agent's patch has real line numbers; the fragments are the fallback (old runs, native edit_file).
      const full = patch ?? view.patch;
      parts.push(full
        ? <Diff key="diff" patch={full} edits={view.edits} path={view.path} />
        : <Diff key="diff" edits={view.edits} path={view.path} note={truncated} />);
      break;
    }
    case "files":
      parts.push(<ul key="files" className={`${box} space-y-0.5`}>{view.changes.map((c, i) =>
        <li key={i}><span className="text-faint">{c.kind}</span> <span className="break-all text-fg">{c.path}</span></li>)}</ul>);
      break;
    case "search":
      if (!failed) parts.push(<Output key="out" text={content ?? "…"} />);
      break;
    default:
      if (argsPreview(args)) parts.push(<CodeView key="args" code={JSON.stringify(args, null, 2)} lang="json" />);
      if (!failed) parts.push(<Output key="out" text={content ?? "…"} />);
  }
  return <div className="ml-3.5 mt-1 space-y-1">{parts}</div>;
}

export default function ToolCall({ call, result }: { call: RunEvent; result?: RunEvent }) {
  const name = String(call.payload.name ?? "tool");
  const args = call.payload.args;
  const view = toolView(name, args);
  const patch = typeof result?.payload.patch === "string" ? result.payload.patch : undefined;
  const status = !result ? "running" : result.payload.status === "error" ? "failed" : "returned";
  return <details className="group/call rounded-ui">
    <summary className="flex cursor-pointer list-none items-start gap-2 rounded-ui px-1.5 py-1 leading-relaxed hover:bg-sunken [&::-webkit-details-marker]:hidden">
      <span className="select-none text-accent" aria-hidden>$</span>
      <span className="min-w-0 flex-1 break-words"><Summary name={name} args={args} view={view} patch={patch} /></span>
      <span className={`mt-0.5 h-1.5 w-1.5 shrink-0 self-center rounded-full ${status === "running" ? "animate-pulse bg-warn" : status === "failed" ? "bg-danger" : "bg-ok"}`} title={status} aria-hidden />
    </summary>
    <Body args={args} view={view} result={result} />
  </details>;
}

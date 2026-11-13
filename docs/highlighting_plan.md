# Plan: syntax highlighting, edit diffs and a richer run log in the web UI

Status: ready to implement. Written 2026-11-13 against the fork at `2a81cec`
(`git.teralink.net/self/openbot`), the plugin `openbot-pi` at `cfd6eb4`, and Pi 1.5.0 as
deployed on the `openbot` host.

## Goal

1. Fenced code blocks in thread messages (`MarkdownContent`) are syntax highlighted.
2. The run log (`RunCard`) shows each tool call in a readable form for that tool, not as
   `name({"json":...})`: shell commands as commands, file paths as paths, edits as a
   highlighted diff, and highlighted file contents where they help.
3. Light, dark and system themes all work, and follow the in-app toggle.
4. More of what the agent CLIs report reaches the run log: reasoning, subagent calls, live tool
   output, compaction/retry/permission notices, and todo lists (WP7).

Out of scope: split diff view, expanding hunks to see more of the file, comments on lines,
accept/reject buttons, and thread renaming (a separate small task).

## Decisions already made

| Topic | Decision | Why |
|---|---|---|
| Highlighter | **Shiki** (`shiki@^4`, `shiki/core` + JS regex engine, no WASM) | VS Code grammar quality, two themes in one render, the same engine `@pierre/diffs` uses, so its chunks are downloaded once. |
| Edit diffs | **`@pierre/diffs`** (`@pierre/diffs/react`, `PatchDiff` / `MultiFileDiff`), **only behind our own lazy `EditDiff` wrapper** | Polished unified diff with word-level highlighting for free. The wrapper keeps Pierre's large, fast-moving API (1.0 in Dec 2025, 115 releases since) out of the rest of the code, so it can be swapped for our own renderer without other changes. |
| Message code blocks | Shiki directly, not Pierre's `File` component | `File` is meant for whole files (Shadow DOM, header, line numbers per block); it does not fit inline snippets. |
| Where diffs come from | The CLIs' own patches, sent through as a new `patch` field on `tool_result` | Pi and Claude already compute exact patches with line numbers and context. Diffing the fragments ourselves is only a fallback. |

Measured cost (Vite 8 test build, gzip, on top of React's 58 KB): Pierre's own code 89 KB, plus
Shiki core and engine 58 KB on first use, plus ~16 KB per language and ~5 KB for both themes.
All of it must be lazy-loaded, so the main bundle (160 KB gz today) does not grow. The Vite
build emits ~400 lazy chunks (every Shiki language/theme Pierre can resolve), about 12 MB on
disk and in the Electron package, but only the needed ones are downloaded.

## Current state (for orientation)

- `frontend/src/components/MarkdownContent.tsx`: `marked` (gfm, breaks) → DOMPurify with
  `FORBID_ATTR: ["style", ...]` → `dangerouslySetInnerHTML`. No highlighter.
- `frontend/src/components/RunCard.tsx:48-71`: every `tool_call` is rendered as
  `$ {name}({JSON.stringify(args)})`, with the matching `tool_result.content` as a raw `<pre>`
  folded beneath it. Results are matched by `payload.tool_call_id === call.payload.id`.
- Run events: `RunEvent { type, payload }` (`frontend/src/api/types.ts:16`). Event payloads are
  stored as JSON, so adding a field needs no migration.
- CLI events are recorded in `backend/openbot/runtime/cli_agents/run.py` `on_event`
  (around line 84): `tool_result` is stored as `{**payload, "content": content[:TOOL_RESULT_CAP]}`
  (`TOOL_RESULT_CAP = 4000`, `runtime/runner.py:76`). Extra payload keys already pass through
  untouched.
- Theme: `frontend/src/lib/theme.ts` (`Theme = "system" | "light" | "dark"`, stored in
  localStorage, mirrored to `<html data-theme>`). CSS tokens in `frontend/src/index.css`
  (`--canvas`, `--surface`, `--sunken`, `--line`, `--fg`, `--muted`, `--faint`, `--ok`,
  `--danger`, …). The dark theme is applied by `:root[data-theme="dark"]` and by
  `@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) }`.
- Tests: vitest with jsdom (`RunCard.test.tsx`, `MarkdownContent.test.tsx`); backend pytest
  (`backend/.venv/bin/pytest -q`; currently 860 passed / 58 skipped). The Pi adapter has its own
  tests in `openbot-pi/tests/test_pi.py`.

### Tool shapes we have to handle

| Kind | Pi (`openbot-pi`) | Claude (`claude_code.py`) | Codex (`openbot-codex`) | Native bots (`tools/builtin`) |
|---|---|---|---|---|
| shell | `bash {command, timeout?}` | `Bash {command, description?, timeout?, run_in_background?}` | `command_execution` (args = the whole item: `command`, `aggregated_output`, `exit_code`, `status`) | `run_shell {command, cwd?, timeout?}` |
| read | `read {path, offset?, limit?}` | `Read {file_path, offset?, limit?}` (result text has `N→` line prefixes) | none | `read_file {path, ...}` |
| write | `write {path, content}` | `Write {file_path, content}` | via `file_change` | `write_file {path, content}` |
| edit | `edit {path, edits: [{oldText, newText}]}` (older form: top-level `oldText`/`newText`) | `Edit {file_path, old_string, new_string, replace_all?}` | via `file_change` | `edit_file {path, old_string, new_string, replace_all?}` |
| search/list | `grep`, `find`, `ls` | `Grep`, `Glob` | none | `search_code {pattern, path?, glob?}`, `list_files {path?, depth?}` |
| file change | none | none | `file_change` (item with `changes: [{path, kind}]`; **verify the shape against a captured stream**, the adapter passes the whole item as args) | none |

Where the patch comes from:

- **Pi 1.5.0**: the `edit` tool returns `details: { diff, patch, firstChangedLine }`
  (`dist/core/tools/edit.js:135`). `patch` is a standard unified patch from
  `Diff.createTwoFilesPatch(path, path, old, new, …, {context: 4, headerOptions: FILE_HEADERS_ONLY})`.
  JSON mode prints `tool_execution_end` unchanged (`dist/modes/json-event.js`), so
  `event.result.details.patch` is in the stream. `openbot_pi` currently keeps only
  `text_content(event["result"])` (`openbot-pi/src/openbot_pi/__init__.py:53-56`).
- **Claude Code**: `user` stream events carry a top-level `tool_use_result`, the tool's full
  output object (Agent SDK 0.3.322 `sdk.d.ts`: `tool_use_result?: unknown`). For `Edit` it is
  `FileEditOutput { filePath, oldString, newString, originalFile, structuredPatch: [{oldStart,
  oldLines, newStart, newLines, lines[]}], gitDiff?: {patch, ...}, ... }`. For `Write` it is
  `FileWriteOutput { type: "create"|"update", filePath, content, structuredPatch, ... }`.
  `claude_code.py:135-141` reads only `message.content` today. **Verify against a real
  stream from the deployed Claude CLI version** before relying on it.
- **Codex**: no diff content as far as known. Show the file list only.
- **Native `edit_file`**: returns a string, no patch. Use the fragment fallback.

## Data contract change

`tool_result` payload gains two optional fields:

```jsonc
{
  "tool_call_id": "…", "name": "edit", "status": "success", "content": "…",
  "patch": "--- src/x.ts\n+++ src/x.ts\n@@ -10,7 +10,7 @@\n …",  // optional, unified diff text
  "patch_truncated": true                                         // optional, only when cut
}
```

- `patch` is always unified-diff **text**, whatever the source, so the frontend has one input format.
- The cap is `PATCH_CAP = 64_000` characters, enforced centrally in `run.py`. If a patch is
  longer, drop it entirely and set `patch_truncated: true`. Never cut a patch in the middle,
  because a broken hunk header is worse than none. The frontend then falls back to the
  fragment diff, with a note.
- Do not add the patch to `activity.record(...)` previews.

## Work packages

Each package is one commit (or a few). The order matters only where noted.

### WP1: backend core (openbot repo)

1. `runtime/runner.py`: add `PATCH_CAP = 64_000` next to `TOOL_RESULT_CAP`.
2. `runtime/cli_agents/run.py` `on_event` `tool_result` branch: if `payload.get("patch")` is a
   str longer than `PATCH_CAP`, replace it with `patch_truncated=True`. Leave everything else
   as it is.
3. `runtime/cli_agents/claude_code.py` `ClaudeStream.feed`, `kind == "user"` branch: read
   `event.get("tool_use_result")`. For each `tool_result` block, if the call's tool name is
   `Edit`/`Write`/`MultiEdit` (track names in `_tool_names` as now) and the output is a dict:
   - prefer `gitDiff.patch` if it is a non-empty str;
   - else build unified text from `structuredPatch`: `--- {filePath}\n+++ {filePath}\n` then
     per hunk `@@ -{oldStart},{oldLines} +{newStart},{newLines} @@\n` + `"\n".join(lines)` + `"\n"`;
   - for a `Write` with `type == "create"` and empty `structuredPatch`, build an all-added patch
     (`--- /dev/null`, one hunk `@@ -0,0 +1,N @@`, every line prefixed `+`).
   Attach the result as `patch`. If a `user` event has several `tool_result` blocks, only attach
   `tool_use_result` when there is exactly one, because the field belongs to the whole event,
   not to one block.
4. Factor the "structuredPatch → unified text" step into a small pure function
   (`cli_agents/patch.py`, `def unified_from_hunks(path, hunks) -> str`) so it can be tested on
   its own.
5. Tests (`backend/tests/`):
   - `unified_from_hunks` round trip: feed the result to `unidiff`-style parsing, or simply
     assert the exact text for a two-hunk fixture.
   - `ClaudeStream.feed` with an `assistant` `tool_use` (Edit) followed by a `user` event that
     has `tool_use_result.structuredPatch` → `tool_result` payload has `patch`.
   - Same with `gitDiff.patch` present → it is preferred.
   - The `run.py` cap: a 70 000-character patch is replaced by `patch_truncated: True`.
6. `docs/plugins.md` (stream parser section): document the optional `patch` field, so plugins
   (Pi, Codex) can emit it.

### WP2: Pi plugin (openbot-pi repo; needs WP1 for the cap, but works without it)

1. `PiStream.feed`, `tool_execution_end`: `details = (event.get("result") or {}).get("details") or {}`.
   If `isinstance(details.get("patch"), str)` and it is non-empty, add `"patch": details["patch"]`.
   Applies to `edit` (and to `write` if a future Pi version adds a patch there; no tool name check).
2. Test in `tests/test_pi.py`: a captured-shape `tool_execution_end` with
   `result: {content: [{type: "text", text: "Successfully replaced 1 block(s) in a.py."}],
   details: {diff: "...", patch: "--- a.py\n+++ a.py\n@@ ...", firstChangedLine: 3}}`
   → payload has `patch`; an event without details → no `patch` key.
3. Optional: capture one real `pi --mode json` edit on the host (as the service user, in a
   scratch dir) and keep it as a fixture.

### WP3: highlighter module (frontend)

Dependencies: `pnpm add shiki@^4 @pierre/diffs@^1.6` (pin exact versions in the lockfile). Pierre
lists `shiki ^3 || ^4` as a dependency, so check `pnpm why shiki` shows **one** copy.

`frontend/src/lib/highlight.ts`:

```ts
// One lazily created Shiki highlighter shared by markdown, tool output and anything else.
export type Highlighter = { html(code: string, lang: string): string | null; has(lang: string): boolean };
export function loadHighlighter(): Promise<Highlighter>;   // memoized promise
export function useHighlighter(): Highlighter | null;      // null until loaded; triggers the load
export function langFromPath(path: string): string | null; // ".ts" → "typescript", "Dockerfile" → "docker", …
```

- `createHighlighterCore` from `shiki/core`, `createJavaScriptRegexEngine` from
  `shiki/engine/javascript`, themes `github-light` + `github-dark` (from `@shikijs/themes/...`,
  or `shiki/themes/...` depending on what Shiki 4 exposes; check), loaded with `import()` so
  Vite splits them out.
- Languages, loaded with `import()` the same way: `typescript tsx javascript jsx json jsonc python
  bash shellscript nix markdown yaml toml rust go elixir css html sql diff dockerfile ini xml`.
  Aliases: `ts js py sh shell zsh console yml md` (Shiki resolves most; map the rest in `has`).
  Load only on first use: start loading when the first code block or tool view mounts.
- `html()` calls `codeToHtml(code, { lang, themes: { light: "github-light", dark: "github-dark" },
  defaultColor: false })`, returns `null` for unknown languages (the caller then renders plain text).
  With `defaultColor: false` every span carries `style="--shiki-light:#…;--shiki-dark:#…"` and
  the colours are chosen in CSS (below). Do not use the themes' background colours; use ours.
- CSS in `index.css` (after the theme blocks):

  ```css
  .shiki, .shiki span { color: var(--shiki-light); }
  :root[data-theme="dark"] .shiki, :root[data-theme="dark"] .shiki span { color: var(--shiki-dark); }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) .shiki, :root:not([data-theme="light"]) .shiki span { color: var(--shiki-dark); }
  }
  .shiki { background: transparent !important; }
  ```

- Security: Shiki builds the HTML from text and escapes it. The only inputs are the code text
  and a language id, and the id is checked against our list. So its output is trusted and is
  inserted **after** DOMPurify (WP4), which keeps `style` forbidden for message content.
- Unit test (`highlight.test.ts`, jsdom): `html("const x = 1", "ts")` contains `--shiki-dark` and
  the escaped text; `html("<b>", "ts")` does not contain a raw `<b>` element; an unknown lang returns `null`;
  `langFromPath` table test.

### WP4: code blocks in messages (frontend; needs WP3)

`MarkdownContent.tsx`:

1. A custom `marked` renderer for `code` (use `marked.use({ renderer: { code(token) {...} } })` on a
   local `Marked` instance, not the global one) emits a placeholder
   `<pre data-ob-code="N"></pre>` and pushes `{ lang, text }` into a per-render array. This works
   for code blocks nested in lists and blockquotes too.
2. Sanitize as today (`data-*` attributes survive DOMPurify by default; assert it in a test).
3. Replace each `<pre data-ob-code="N"></pre>` in the sanitized string with:
   - the highlighter's HTML when `useHighlighter()` has loaded and the lang is known, wrapped as
     `<div class="code-block" data-lang="ts">…shiki pre…</div>`;
   - otherwise `<pre><code>{escaped text}</code></pre>` (the same as today), so nothing waits for the highlighter.
   Escape with a small `escapeHtml` helper. The placeholder index is the only thing taken from
   the sanitized string.
4. `useMemo` the parse on `[content, highlighter]`. Messages re-render when the highlighter loads.
5. Optional, small: a copy button on code blocks (a click handler on the container,
   `closest(".code-block")`).
6. Style: `.markdown-content .code-block pre` gets the existing code-block look (border `--line`,
   background `--sunken`, `text-[12.5px]`, horizontal scroll).
7. Tests: existing ones must still pass (`textContent` contains `const x = 1;`). Add: with the
   highlighter loaded, a ```` ```ts ```` block produces `.shiki` spans; a ```` ```nope ```` block stays
   plain; a code block containing `</pre><img src=x onerror=…>` renders as text, with no `img`
   element; a code block inside a list item is highlighted.

### WP5: per-tool views in the run log (frontend; needs WP3; the edit view needs WP6)

Split `RunCard.tsx`'s per-call rendering into `frontend/src/components/runlog/`:

- `toolViews.ts`: normalize a call into one of these views:

  ```ts
  type ToolView =
    | { kind: "shell"; command: string; cwd?: string }
    | { kind: "read"; path: string; range?: string }
    | { kind: "write"; path: string; content: string }
    | { kind: "edit"; path: string; edits: { old: string; new: string }[] }
    | { kind: "search"; pattern: string; where?: string }
    | { kind: "files"; changes: { path: string; kind: string }[] }  // Codex file_change
    | { kind: "generic" };
  export function toolView(name: string, args: Record<string, unknown>): ToolView;
  ```

  Match on the lower-cased name with a table built from the tool shapes table above:
  `bash|run_shell|command_execution` → shell (Codex: `args.command`), `read|read_file` → read,
  `write|write_file` → write, `edit|edit_file|multiedit` → edit (Pi `edits[]` or top-level
  `oldText/newText`; Claude/native `old_string/new_string`; MultiEdit `edits[].old_string/new_string`),
  `grep|glob|find|ls|search_code|list_files` → search, `file_change` → files. If an expected field
  is missing or has the wrong type, return `generic`. Never throw: plugins and MCP tools send arbitrary args.
- `ToolCallSummary.tsx`: the `<summary>` line. Keep the `$` prompt and the status dot.
  - shell: `$ <command>`, highlighted as `bash`; first line only in the summary, the full
    command in the body when it spans several lines.
  - read: `read <path>` plus `:120-180` when offset/limit are given.
  - write/edit: `write <path>` / `edit <path>` plus `+N −M` counts once the result has a patch
    (count from the patch text).
  - search: `grep <pattern> in <where>`.
  - generic: `name(…)` with a short one-line preview of the args (truncated at ~120 chars).
- `ToolCallBody.tsx`: what is shown when the call is expanded.
  - shell: the result as plain `<pre>` (strip ANSI escapes with a small regex). No language highlighting.
  - read: result highlighted by `langFromPath(path)`. For Claude's `Read`, strip the
    `^\s*\d+→` line prefixes before highlighting (detect: every non-empty line matches). Keep the
    existing `max-h-60` scroll.
  - write: if `result.patch` → `EditDiff`; else highlighted `content`.
  - edit: `EditDiff` (WP6) with `patch` if present, else the fragments.
  - files: list of paths with their kind.
  - generic: args as `JSON.stringify(args, null, 2)` highlighted as `json`, then the result as
    today.
  - Errors (`status === "error"`): always show the result text in the danger style, above any diff.
- Keep the existing behaviour: collapsed by default, the scratchpad refetch logic, the counts in the header.
- Tests (`RunCard.test.tsx` and a new `toolViews.test.ts`): a table test for `toolView` across Pi,
  Claude, Codex and native shapes (fixtures from the table above) including malformed args;
  RunCard renders `$ ls -la` for a Pi `bash` call and for a Claude `Bash` call; an unknown tool still
  shows its name and args; existing tests pass unchanged.

### WP6: `EditDiff` wrapper around Pierre (frontend; needs WP3 for shared Shiki only)

`frontend/src/components/runlog/EditDiff.tsx` is the default export used through
`React.lazy(() => import("./EditDiff"))` with a `<Suspense>` fallback, which is a plain `<pre>`
of the patch (or of the fragments).

```ts
type Props =
  | { patch: string; path?: string }
  | { path: string; edits: { old: string; new: string }[] };   // fallback, no line numbers
```

- With `patch`: `<PatchDiff patch={patch} options={opts} />`.
- With fragments: one `<MultiFileDiff oldFile={{ name: path, contents: old }} newFile={{ name: path,
  contents: new }} options={{ ...opts, disableLineNumbers: true }} />` per edit (line numbers would
  be relative to the fragment, so wrong). Show a small "fragment, no line numbers" note.
- `opts` (all from `@pierre/diffs` `BaseDiffOptions`): `diffStyle: "unified"`,
  `lineDiffType: "word"` (try `"word-alt"` too and keep whichever reads better), `overflow: "wrap"`,
  `theme: { light: "github-light", dark: "github-dark" }` (the same themes as WP3, so the colours match),
  `themeType` from a `useThemeType()` hook, `disableFileHeader: true` (the summary line already
  names the file).
- `useThemeType()` (`lib/theme.ts`): `useSyncExternalStore` over a `MutationObserver` on
  `document.documentElement`'s `data-theme`. It returns `"light" | "dark" | "system"`, which is exactly
  Pierre's `ThemeTypes`. `ThemeToggle` keeps working as it does.
- Styling: Pierre renders into a Shadow DOM, so Tailwind classes do not reach inside. Map our tokens
  through its CSS custom properties on the host element: `--diffs-font-family` (IBM Plex Mono, as
  used elsewhere), `--diffs-bg` → `var(--canvas)`, and the separator, line number and gutter colours
  from `--line`/`--faint`. The full list is the `--diffs-*` variables in the package's `dist/style.js`
  (about 60). Use `unsafeCSS` only for anything the variables cannot reach. Keep the result in a
  `max-h-80 overflow-auto` container, the same as other tool output.
- Worker pool: not needed (small hunks). Pass `disableWorkerPool` if the component warns without it.
- Nothing outside `EditDiff.tsx` imports `@pierre/diffs`. Add an oxlint `no-restricted-imports`
  rule for that if oxlint supports it here; otherwise a comment at the top of the file.
- Tests: jsdom has no Shadow DOM layout, so test the wrapper's choices, not Pierre's rendering:
  the right Pierre component gets the right props for a patch / for fragments (mock
  `@pierre/diffs/react`). One manual browser check (WP8).

### WP7: more event types from the agent CLIs (core, plugins, frontend; independent of WP3-WP6 except the views)

Today `StreamParser` emits only `session`, `init`, `delta`, `text`, `tool_call`, `tool_result`
(`cli_agents/base.py:98`), and the frontend renders only `text`, `tool_call`, `tool_result`. Everything
else the CLIs report is thrown away. Inventory (Pi 1.5.0 from its dist, Claude from Agent SDK
0.3.322 types, Codex from the adapter):

| What | Pi | Claude stream-json | Codex exec JSON |
|---|---|---|---|
| Reasoning | `thinking` blocks in `message_end.message.content`; `thinking_delta` in `message_update` | `thinking` blocks in `assistant` content (may be empty/redacted, depending on the CLI's thinking display); `thinking_delta` in `stream_event` | `reasoning` item (`text`) |
| Nested / subagent calls | `parentToolCallId` on `tool_execution_*` | `parent_tool_use_id` on `assistant`/`user` events (the `Task`/`Agent` tool), plus `system` `task_started` / `task_progress` / `task_notification` | none |
| Live tool output | `tool_execution_update` (`partialResult`, e.g. bash output so far) | `tool_progress` (elapsed time) | `item.updated` |
| Todo / plan | none built in | `TodoWrite` tool call (`todos: [{content, status, activeForm}]`) | `todo_list` item (`items: [{text, completed}]`) |
| Context compaction | `compaction_start` / `compaction_end` | `system` `compact_boundary` | none known |
| Retries / limits | `auto_retry_start` / `auto_retry_end` | `system` `api_retry`, `rate_limit_event` | `error` events that are not fatal |
| Permission denied | none | `system` `permission_denied` (relevant for `read-only` bots) | the adapter denies approvals; shows up as a failed item |
| Model fallback | none | `model_refusal_fallback` / `model_refusal_no_fallback` | none |
| Web / MCP | MCP via extensions (disabled in read-only) | `WebSearch`, `WebFetch`, `mcp__<server>__<tool>` tool calls | `web_search` (`query`), `mcp_tool_call` (`server`, `tool`, `arguments`) items |

The current Claude adapter emits `tool_call`s from subagents as if they were top-level calls (it
only filters subagent **text**), and Codex `reasoning` and `todo_list` items are dropped.

New normalized kinds (document them in `StreamParser`'s docstring and `docs/plugins.md`; all optional
for a plugin to emit):

| Kind | Payload | Stored? | Core handling (`run.py` `on_event`) |
|---|---|---|---|
| `thinking` | `str` (final text of one reasoning block) | yes, run event `thinking` | record; skip empty strings |
| `tool_call` / `tool_result` | existing, plus optional `parent_id` | yes | pass through |
| `tool_progress` | `{tool_call_id, content?: str, elapsed_s?: float}` | **no**, bus only | publish `run.event` `tool_progress` like `text_delta`; `content` is the full output so far, capped at `TOOL_RESULT_CAP` |
| `notice` | `{level: "info" \| "warn" \| "error", kind: "compaction" \| "retry" \| "rate_limit" \| "permission_denied" \| "model_fallback" \| str, text: str}` | yes, run event `notice` | record, plus `activity.record(..., level=warning)` for `warn`/`error` |
| `todos` | `[{content: str, status: "pending" \| "in_progress" \| "completed"}]` | in `ThreadTodoList` | upsert the row for (thread, actor) and publish `todos.updated`, the same as the native `todo_write` tool (`tools/builtin/todo.py:122-140`; factor the upsert into a shared helper there). The existing `TodoPanel` then shows CLI agents' plans with no frontend change. |

Adapter work:

- **Claude (`claude_code.py`)**: emit `thinking` for non-empty `thinking` blocks on the main chain;
  set `parent_id = parent_tool_use_id` on `tool_call`s and `tool_result`s from subagents (stop
  presenting them as top-level); `todos` from a `TodoWrite` `tool_use` (map `content`/`status`, drop
  `activeForm`), while still emitting the `tool_call`; `notice` for `compact_boundary`, `api_retry`,
  `rate_limit_event` (only when the status is not "allowed"), `permission_denied`,
  `model_refusal_*`; `tool_progress` from `tool_progress` (elapsed only).
- **Pi (`openbot-pi`)**: `thinking` from `message_end` content blocks of type `thinking`;
  `parent_id` from `parentToolCallId`; `tool_progress` from `tool_execution_update` (text of
  `partialResult`); `notice` for `compaction_*` and `auto_retry_*`. Throttle `tool_progress` to at
  most ~4 per second per call in the adapter.
- **Codex (`openbot-codex`)**: `thinking` from completed `reasoning` items; `todos` from `todo_list`
  items (`completed` → `"completed"`, else `"pending"`; mark the first open one `"in_progress"`);
  `notice` for non-fatal `error` events. Map `web_search` and `mcp_tool_call` to proper `tool_call`
  args (`{query}`, `{server, tool, arguments}`) instead of the whole item.
- Every adapter: unknown events stay ignored. One test per new mapping, with a captured-shape fixture.

Frontend (in `RunCard` / `runlog/`):

- `thinking`: a collapsed "Thinking" row in the run log at its position, muted, rendered as markdown
  when expanded.
- `parent_id`: nest child calls under the parent call (for Claude, the `Task`/`Agent` call shows
  "subagent: <description>" and its children indented). Calls whose parent is not in the run render
  at top level.
- `tool_progress`: while a call has no result yet, show the streamed output (or "running 42 s") in its
  body; drop it when the `tool_result` arrives. Keep it in component state, not in the query cache.
- `notice`: a one-line row with an icon by level (`info` faint, `warn` amber, `error` danger), e.g.
  "Context compacted", "Retrying after rate limit (attempt 2)", "Permission denied: Bash rm -rf …".
- Extra tool views for WP5's `toolView`: `websearch|web_search` → `search the web: <query>`;
  `webfetch` → the URL as a link; `mcp__server__tool` and Codex `mcp_tool_call` → `server · tool` plus
  args as JSON; `task|agent` → the subagent row described above; `todowrite` → a compact checklist
  instead of JSON.
- The header count (`N tool calls`) counts top-level calls only, plus "+M in subagents" when there are any.

Tests: adapter fixtures for each mapping (Claude in the core repo, Pi and Codex in their plugin
repos); `run.py` stores `thinking` and `notice`, does not store `tool_progress`, and turns `todos` into
a `ThreadTodoList` row plus a `todos.updated` event; RunCard renders a nested subagent call, a notice
row and a thinking row; the existing tests pass.

### WP8: verification, size check, docs

1. `pnpm typecheck && pnpm lint && pnpm test` in `frontend/`; `backend/.venv/bin/pytest -q`;
   `ruff check` on changed backend files; `pytest` in `openbot-pi`.
2. `pnpm build`, then compare `dist/assets/index-*.js` gzip size against the current 160 KB.
   **It must not grow by more than a few KB.** Shiki and Pierre must sit in lazy chunks only.
   Check with `grep -l "createHighlighterCore\|PatchDiff" dist/assets/index-*.js` (no hit).
3. Manual check in the browser (dev server against a local backend, or the deployed instance after WP9):
   a Pi thread and a Claude thread where the bot reads a file, runs a command and edits a file;
   switch light / dark / system while a diff is open; a message with a ```` ```python ```` block and
   one with an unknown language; a long line (wrap); an edit that failed (error shown, no diff).
4. Docs: `docs/plugins.md` (`patch` field, WP1), and a short "Run log" note in `docs/architecture.md`
   where the frontend is described (which tools get special views, where the patch comes from).

### WP9: deploy (infrastructure repo; ask the owner first)

1. Push the fork (`origin` = git.teralink.net `self/openbot`), `openbot-pi` and `openbot-codex`.
2. In `~/Documents/infrastructure/nix/pkgs/openbot/sources.nix`, bump `openbot.rev`/`hash`
   (currently `02ccba6…`), `pi.rev`/`hash` (currently `cfd6eb4…`) and `codex.rev`/`hash`
   (currently `79f4f66…`). Get the hashes with
   `nix-prefetch-git` or by building once with a fake hash.
3. The frontend build in Nix uses pnpm deps with a fixed hash. Adding `shiki` and `@pierre/diffs`
   changes the lockfile, so the pnpm deps hash in the openbot package must be updated too
   (find it next to `sources.nix`).
4. Commit, `git push`, then `ssh -t openbot 'nixos update && nixos switch'`. Check
   `systemctl is-active` for the openbot unit and load a thread with a Pi edit.

## Acceptance criteria

- A Pi or Claude edit in the run log shows a unified diff with real line numbers, syntax
  highlighting and word-level changes, in both themes.
- A native `edit_file` call shows a fragment diff without line numbers, not JSON.
- Shell calls read as `$ command`; reads show the path and highlighted contents; unknown tools show
  pretty-printed, highlighted JSON.
- Code blocks in messages are highlighted for the listed languages and plain for the rest; no
  XSS regression (tests in WP4).
- The main JS bundle does not grow by more than a few KB; Shiki and Pierre load only when needed.
- Reasoning, subagent calls (nested), compaction/retry/permission notices and live output of running
  tools are visible in the run log for the CLIs that report them; Claude and Codex plans appear in the
  existing todo panel.
- All existing frontend and backend tests pass.

## Risks and open points

- **Claude `tool_use_result`** comes from the SDK types, not from a captured stream of the deployed
  CLI. Capture one before WP1 step 3. If it is missing, Claude edits use the fragment fallback; nothing breaks.
- **Codex `file_change` shape** is unverified. The view must fall back to `generic` if `changes` is not
  a list of `{path, kind}`.
- **Pierre API churn**: 1.7 is in beta. Pin the exact version; all usage is in `EditDiff.tsx`.
  If theming through the Shadow DOM fights back, replace `EditDiff` with our own renderer
  (`jsdiff` `parsePatch` + per-line Shiki tokens + `diffWordsWithSpace`, est. 250-350 lines). The
  props stay the same, so the rest of the code does not change.
- **Bundle bloat**: Pierre resolves languages through `shiki/langs`, so the build emits every
  language as a lazy chunk (~12 MB on disk, also in the Electron package). That is accepted; only
  used chunks are downloaded.
- **Event inventory for WP7** comes from Pi's dist, the Agent SDK's types and the Codex adapter,
  not from captured streams. Capture one real run per CLI (with an edit, a subagent or todo where
  possible) and keep the files as test fixtures before writing the mappings.
- **Claude thinking** may arrive empty or redacted, depending on the CLI version and settings. Show
  nothing in that case, not an empty row.
- **Todos from CLIs overwrite** the bot's `ThreadTodoList` row, the same as native `todo_write`. That is
  intended (one list per bot per thread), but the list survives into later turns of a native bot
  using the same actor. Not an issue today, since a bot is either native or CLI.
- **Old runs** have no `patch` field. They get the fragment fallback automatically.
- **Upstream**: if this goes upstream (regnull/openbot), WP1 and the `patch` field are the part
  worth proposing first; the UI choice (Pierre) may be a harder sell.

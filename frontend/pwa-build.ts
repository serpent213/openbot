import { createHash } from "node:crypto";
import { readFileSync, readdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import type { Plugin } from "vite";

/** No PWA runtime dependency. Generate from the finished bundle, including plugin
 * assets, but precache only the entry shell (not labs or lazy route chunks). */
export function pwaBuild(): Plugin {
  let out: string;
  let root: string;
  return {
    name: "openbot-pwa",
    apply: "build",
    configResolved(config) { root = config.root; out = resolve(root, config.build.outDir); },
    closeBundle() {
      const html = readFileSync(resolve(out, "index.html"), "utf8");
      const template = readFileSync(resolve(root, "service-worker.js"), "utf8");
      const shell = ["/index.html", "/offline.html", "/theme-init.js", "/manifest.webmanifest", "/favicon.svg", "/apple-touch-icon.png", "/icon-192.png", "/icon-512.png", "/icon-maskable-512.png"];
      const assets = readdirSync(resolve(out, "assets"), { recursive: true }).map(String)
        .filter((name) => /\.(js|css|woff2?)$/.test(name)).map((name) => `/assets/${name.replaceAll("\\", "/")}`).sort();
      const entry = [...html.matchAll(/(?:src|href)="(\/assets\/[^"?#]+)"/g)].map((m) => m[1]);
      const precache = [...new Set([...shell, ...entry])].sort();
      const publicAssets = [...new Set([...shell.filter((p) => !p.endsWith(".html")), ...assets])].sort();
      const hash = createHash("sha256").update(template).update(JSON.stringify(publicAssets));
      for (const path of precache) hash.update(readFileSync(resolve(out, path.slice(1))));
      const worker = template.replaceAll("__BUILD_ID__", hash.digest("hex").slice(0, 20))
        .replace("__PUBLIC_ASSETS__", JSON.stringify(publicAssets)).replace("__PRECACHE__", JSON.stringify(precache));
      writeFileSync(resolve(out, "sw.js"), worker);
    },
  };
}

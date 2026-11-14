import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { expect, it } from "vitest";
import { pwaBuild } from "../../pwa-build";

it("generates deterministic, versioned public allowlists without precaching lazy labs", () => {
  const root = mkdtempSync(join(tmpdir(), "pwa-build-"));
  const out = join(root, "dist");
  mkdirSync(join(out, "assets"), { recursive: true });
  try {
    writeFileSync(join(root, "service-worker.js"), readFileSync("service-worker.js"));
    for (const name of ["offline.html", "theme-init.js", "manifest.webmanifest", "favicon.svg", "apple-touch-icon.png", "icon-192.png", "icon-512.png", "icon-maskable-512.png", "assets/entry-12345678.js", "assets/lab-12345678.js", "assets/model.glb"]) writeFileSync(join(out, name), "fixture");
    writeFileSync(join(out, "index.html"), '<script src="/assets/entry-12345678.js"></script>');
    const plugin = pwaBuild();
    (plugin.configResolved as any)({ root, build: { outDir: "dist" } });
    (plugin.closeBundle as any)();
    const first = readFileSync(join(out, "sw.js"), "utf8");
    expect(first).not.toContain("__BUILD_ID__");
    expect(first).toContain("/assets/lab-12345678.js");
    expect(first).not.toContain("/assets/model.glb");
    const precache = JSON.parse(first.match(/const PRECACHE = (.*);/)![1]);
    expect(precache).toContain("/assets/entry-12345678.js");
    expect(precache).not.toContain("/assets/lab-12345678.js");
    (plugin.closeBundle as any)();
    expect(readFileSync(join(out, "sw.js"), "utf8")).toBe(first);
    writeFileSync(join(out, "theme-init.js"), "changed");
    (plugin.closeBundle as any)();
    expect(readFileSync(join(out, "sw.js"), "utf8")).not.toBe(first);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

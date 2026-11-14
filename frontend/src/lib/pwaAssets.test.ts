import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const publicFile = (name: string) => readFileSync(new URL(`../../public/${name}`, import.meta.url));
describe("install identity", () => {
  it("uses same-origin launch links without credentials and standalone display", () => {
    const manifest = JSON.parse(publicFile("manifest.webmanifest").toString());
    expect(manifest).toMatchObject({ id: "/", scope: "/", start_url: "/inbox", display: "standalone", name: "OpenBot" });
    expect(manifest.icons.some((i: { purpose: string }) => i.purpose === "maskable")).toBe(true);
    for (const link of [manifest.start_url, ...manifest.shortcuts.map((s: { url: string }) => s.url)]) {
      expect(link).toMatch(/^\/[a-z]+$/);
    }
  });
  it("ships real PNGs at each advertised size, including the iOS touch icon", () => {
    for (const [name, size] of [["apple-touch-icon", 180], ["icon-192", 192], ["icon-512", 512], ["icon-maskable-512", 512]] as const) {
      const png = publicFile(`${name}.png`);
      expect(png.subarray(1, 4).toString()).toBe("PNG");
      expect(png.readUInt32BE(16)).toBe(size);
      expect(png.readUInt32BE(20)).toBe(size);
    }
    const html = readFileSync(new URL("../../index.html", import.meta.url), "utf8");
    expect(html).toContain('href="/apple-touch-icon.png"');
    expect(html).toContain('href="/manifest.webmanifest"');
  });
});

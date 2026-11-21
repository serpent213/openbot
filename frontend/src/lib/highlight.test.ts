// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { highlight, langFromPath, loadLang, resolveLang } from "./highlight";

describe("highlight", () => {
  it("returns null until a language has loaded, then dual-theme HTML", async () => {
    expect(highlight("const x = 1", "rust")).toBeNull();
    await loadLang("ts");
    const html = highlight("const x = 1", "ts")!;
    expect(html).toContain('class="shiki');
    expect(html).toContain("--shiki-light:");
    expect(html).toContain("--shiki-dark:");
    expect(html).toContain("const");
  });

  it("escapes the code", async () => {
    await loadLang("typescript");
    const box = document.createElement("div");
    box.innerHTML = highlight('const s = "<b onclick=alert(1)>x</b>"', "typescript")!;
    expect(box.querySelector("b")).toBeNull();
    expect(box.textContent).toContain('"<b onclick=alert(1)>x</b>"');
  });

  it("leaves unknown languages plain", async () => {
    await loadLang("brainfuck");
    expect(highlight("+++", "brainfuck")).toBeNull();
    expect(highlight("x", "constructor")).toBeNull();
    expect(resolveLang("__proto__")).toBeNull();
  });

  it("resolves aliases", () => {
    expect(resolveLang("TS")).toBe("typescript");
    expect(resolveLang(" bash ")).toBe("shellscript");
    expect(resolveLang("yml")).toBe("yaml");
    expect(resolveLang("nix")).toBe("nix");
    expect(resolveLang("")).toBeNull();
    expect(resolveLang(undefined)).toBeNull();
  });

  it.each([
    ["src/components/RunCard.tsx", "tsx"], ["backend/openbot/main.py", "python"], ["flake.nix", "nix"],
    ["Dockerfile", "docker"], ["docker/Dockerfile.dev", "docker"], ["C:\\work\\a.RS", "rust"],
    ["config.yml", "yaml"], ["pyproject.toml", "toml"], ["README", null], ["archive.tar.gz", null],
    ["", null], [".envrc", "shellscript"], ["lib/app.ex", "elixir"],
  ])("langFromPath(%s) = %s", (path, lang) => {
    expect(langFromPath(path)).toBe(lang);
  });
});

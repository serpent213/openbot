// @vitest-environment jsdom
import { expect, it, vi } from "vitest";
import { trackViewport } from "./viewport";
it("tracks keyboard viewport height/offset, leaves pinch zoom alone, and cleans up", () => {
  const viewport = Object.assign(new EventTarget(), { height: 800, offsetTop: 0, scale: 1 });
  vi.stubGlobal("visualViewport", viewport);
  const stop = trackViewport();
  try {
    expect(document.documentElement.style.getPropertyValue("--app-height")).toBe("800px");
    viewport.height = 400; viewport.offsetTop = 20;
    viewport.dispatchEvent(new Event("resize"));
    expect(document.documentElement.style.getPropertyValue("--app-height")).toBe("400px");
    expect(document.documentElement.style.getPropertyValue("--app-top")).toBe("20px");
    viewport.scale = 2; viewport.height = 200;
    viewport.dispatchEvent(new Event("resize"));
    expect(document.documentElement.style.getPropertyValue("--app-height")).toBe("400px");
  } finally { stop(); vi.unstubAllGlobals(); }
  expect(document.documentElement.style.getPropertyValue("--app-height")).toBe("");
});

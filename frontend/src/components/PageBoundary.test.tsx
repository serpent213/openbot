// @vitest-environment jsdom
import { act, lazy, Suspense } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import PageBoundary from "./PageBoundary";
(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
it("contains failed lazy imports without automatically reloading or losing the shell", async () => {
  const error = vi.spyOn(console, "error").mockImplementation(() => {});
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  const Page = lazy(async () => { throw new TypeError("offline chunk"); });
  const node = document.createElement("div");
  const root = createRoot(node);
  try {
    await act(async () => root.render(<><nav>Navigation remains</nav><PageBoundary><Suspense fallback="Loading"><Page /></Suspense></PageBoundary></>));
    expect(node.querySelector('[role="alert"]')?.textContent).toContain("could not be loaded");
    expect(node.querySelector("nav")?.textContent).toBe("Navigation remains");
    expect(confirm).not.toHaveBeenCalled();
    await act(async () => node.querySelector("button")!.click());
    expect(confirm).toHaveBeenCalledOnce();
  } finally { act(() => root.unmount()); error.mockRestore(); confirm.mockRestore(); }
});

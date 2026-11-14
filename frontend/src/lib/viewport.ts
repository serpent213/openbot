/** dvh handles browser chrome; VisualViewport also accounts for an overlaid iOS
 * keyboard. Leave pinch zoom alone: resizing then would fight accessibility zoom. */
export function trackViewport(): () => void {
  const viewport = window.visualViewport;
  if (!viewport) return () => {};
  const root = document.documentElement;
  const update = () => {
    if (viewport.scale !== 1) return;
    root.style.setProperty("--app-height", `${viewport.height}px`);
    root.style.setProperty("--app-top", `${viewport.offsetTop}px`);
  };
  update();
  viewport.addEventListener("resize", update);
  viewport.addEventListener("scroll", update);
  return () => {
    viewport.removeEventListener("resize", update);
    viewport.removeEventListener("scroll", update);
    root.style.removeProperty("--app-height");
    root.style.removeProperty("--app-top");
  };
}

/** Mobile OSes can suspend a page without closing its sockets. Reconcile on return,
 * online, and BFCache restore. Coalesce the events a single resume can generate. */
export function onPageResume(callback: () => void): () => void {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const resume = () => {
    if (document.visibilityState === "hidden" || navigator.onLine === false) return;
    clearTimeout(timer);
    timer = setTimeout(callback, 100);
  };
  const restored = (event: PageTransitionEvent) => { if (event.persisted) resume(); };
  document.addEventListener("visibilitychange", resume);
  window.addEventListener("online", resume);
  window.addEventListener("pageshow", restored);
  return () => {
    clearTimeout(timer);
    document.removeEventListener("visibilitychange", resume);
    window.removeEventListener("online", resume);
    window.removeEventListener("pageshow", restored);
  };
}

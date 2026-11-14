import { useEffect, useRef, useState } from "react";
import { onPageResume } from "../lib/resume";

/** Optional browser enhancement, never registered in Vite dev or Electron. */
export default function PwaUpdates() {
  const [update, setUpdate] = useState<{ waiting?: ServiceWorker; changed?: boolean } | null>(null);
  const reloadApproved = useRef(false);
  useEffect(() => {
    if (!import.meta.env.PROD || window.openbotDesktop?.isElectron || !window.isSecureContext || !("serviceWorker" in navigator)) return;
    let disposed = false;
    let stopResume = () => {};
    let stopFound = () => {};
    const observed = new Map<ServiceWorker, () => void>();
    let hadController = !!navigator.serviceWorker.controller;
    const changed = () => {
      if (reloadApproved.current) { window.location.reload(); return; }
      if (hadController) setUpdate({ changed: true });
      hadController = true;
    };
    navigator.serviceWorker.addEventListener("controllerchange", changed);
    void navigator.serviceWorker.register("/sw.js", { scope: "/", updateViaCache: "none" }).then((registration) => {
      if (disposed) return;
      const waiting = () => { if (registration.waiting) setUpdate({ waiting: registration.waiting }); };
      const found = () => {
        const worker = registration.installing;
        if (!worker || observed.has(worker)) return;
        const state = () => { if (worker.state === "installed") waiting(); };
        worker.addEventListener("statechange", state);
        observed.set(worker, state);
      };
      waiting(); found();
      registration.addEventListener("updatefound", found);
      stopFound = () => registration.removeEventListener("updatefound", found);
      let lastCheck = Date.now();
      stopResume = onPageResume(() => {
        if (Date.now() - lastCheck < 60_000) return;
        lastCheck = Date.now();
        void registration.update().catch(() => {});
      });
    }).catch(() => { /* App stays usable if SW/storage/policy is unavailable. */ });
    return () => {
      disposed = true;
      stopResume(); stopFound();
      for (const [worker, listener] of observed) worker.removeEventListener("statechange", listener);
      navigator.serviceWorker.removeEventListener("controllerchange", changed);
    };
  }, []);
  if (!update) return null;
  return <div role="status" className="fixed bottom-4 right-4 z-50 max-w-sm rounded-ui border border-line bg-surface p-3 text-sm shadow-lg">
    OpenBot update available. Reload when ready.
    <button type="button" className="ml-2 underline" onClick={() => {
      if (!window.confirm("Reload OpenBot? Unsaved forms and attached images will be lost. Text drafts are retained only if local draft storage is enabled and available. Server runs continue.")) return;
      if (update.waiting?.state === "installed") {
        reloadApproved.current = true;
        update.waiting.postMessage({ type: "ACTIVATE_UPDATE" });
      } else window.location.reload();
    }}>Reload to update</button>
  </div>;
}

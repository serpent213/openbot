import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { backendAvailableEvent, backendUnavailableEvent } from "../api/errors";
import { onPageResume } from "../lib/resume";

/** Keep the reader's route and draft through transient outages. Only reads are
 * refreshed: reconnecting must never replay a message or operator mutation. */
export default function BackendFallback({ children }: { children: React.ReactNode }) {
  const qc = useQueryClient();
  const [unavailable, setUnavailable] = useState(() => navigator.onLine === false);
  useEffect(() => {
    const down = () => setUnavailable(true);
    const up = () => setUnavailable(false);
    const resume = onPageResume(() => { void qc.invalidateQueries(); });
    window.addEventListener(backendUnavailableEvent, down);
    window.addEventListener("offline", down);
    window.addEventListener(backendAvailableEvent, up);
    return () => {
      resume();
      window.removeEventListener(backendUnavailableEvent, down);
      window.removeEventListener("offline", down);
      window.removeEventListener(backendAvailableEvent, up);
    };
  }, [qc]);
  return <>
    {unavailable && <div role="status" className="fixed right-3 top-3 z-50 max-w-sm rounded-ui border border-warn bg-surface p-3 text-sm shadow-lg">
      Connection interrupted. Your place is preserved; reconnecting will refresh server state.
      <button type="button" className="ml-2 underline" onClick={() => { void qc.invalidateQueries(); }}>Retry connection</button>
    </div>}
    {children}
  </>;
}

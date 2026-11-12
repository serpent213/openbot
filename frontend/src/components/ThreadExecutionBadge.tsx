import type { SandboxInventory, Thread } from "../api/types";

/** A binding is immutable. Unknown inventory is never presented as a safe local fallback. */
export default function ThreadExecutionBadge({ thread, inventory }: { thread: Thread; inventory?: SandboxInventory }) {
  if (thread.sandbox_id) {
    const box = inventory?.sandboxes.find((item) => item.id === thread.sandbox_id);
    return <span className="text-muted" title={`Fixed sandbox: ${thread.sandbox_id}. Create a new thread to change it.`}>
      Sandbox: {box?.name ?? thread.sandbox_id}{box && box.state !== "active" ? ` (${box.state})` : ""}
    </span>;
  }
  if (thread.host_execution || inventory?.enabled === false) {
    return <span className="font-medium text-danger" title="Not sandboxed. Tools run with the controller account's permissions, not necessarily Unix root. Binding cannot be changed.">Host — unrestricted</span>;
  }
  return <span className="text-warn" title="No sandbox selected. In sandbox mode this thread cannot execute; create a new bound thread.">Unbound</span>;
}

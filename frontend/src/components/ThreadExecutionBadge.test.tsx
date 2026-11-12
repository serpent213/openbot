// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import ThreadExecutionBadge from "./ThreadExecutionBadge";
import type { SandboxInventory, Thread } from "../api/types";

const inventory: SandboxInventory = { enabled: true, sandboxes: [
  { id: "a", name: "Development", resource: "slot1", state: "active", share_global_memory: false },
] };
const markup = (thread: Partial<Thread>, data = inventory) => renderToStaticMarkup(<ThreadExecutionBadge thread={thread as Thread} inventory={data} />);

describe("thread execution labels", () => {
  it("shows the named immutable binding", () => {
    expect(markup({ sandbox_id: "a" })).toContain("Sandbox: Development");
    expect(markup({ sandbox_id: "a" })).toContain("Create a new thread to change it");
  });
  it("warns in red for explicit host or legacy local execution", () => {
    expect(markup({ host_execution: true })).toContain("text-danger");
    expect(markup({ host_execution: true })).toContain("Host — unrestricted");
    expect(markup({}, { ...inventory, enabled: false })).toContain("Host — unrestricted");
  });
  it("never labels unbound or missing sandbox state as a host fallback", () => {
    expect(markup({})).toContain("Unbound");
    expect(markup({ sandbox_id: "gone" })).toContain("Sandbox: gone");
    expect(markup({ sandbox_id: "a" }, { enabled: false, sandboxes: [] })).not.toContain("Host");
  });
});

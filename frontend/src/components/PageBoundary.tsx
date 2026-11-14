import { Component, type ReactNode } from "react";

/** A lazy route may be unavailable offline or after a multi-version deployment.
 * Never reload automatically and discard an operator's edits to recover it. */
export default class PageBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    if (!this.state.failed) return this.props.children;
    return <div role="alert" className="space-y-3 p-4">
      <h1 className="font-semibold">This page could not be loaded</h1>
      <p>You may be offline, or a newer OpenBot version may be available. Reconnect and reload to try again.</p>
      <button type="button" className="underline" onClick={() => {
        if (window.confirm("Reload OpenBot? Unsaved forms, images and text not saved locally will be lost.")) window.location.reload();
      }}>Reload page</button>
    </div>;
  }
}

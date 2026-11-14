import { useState } from "react";
import { deleteSavedDrafts, savesDrafts, setSavesDrafts } from "../lib/draftStorage";
import { Button, Card, Hint, SectionTitle } from "./ui";

export default function DraftSettings() {
  const [enabled, setEnabled] = useState(savesDrafts);
  const [notice, setNotice] = useState("");
  return <Card className="space-y-3">
    <SectionTitle>Local text drafts</SectionTitle>
    <label className="flex items-center gap-2"><input type="checkbox" checked={enabled} onChange={(e) => {
      try { setSavesDrafts(e.target.checked); setEnabled(e.target.checked); setNotice("Draft preference saved."); }
      catch { setNotice("Could not update browser storage. Check browser settings; saved drafts may remain."); }
    }} /> Save text drafts on this device</label>
    <Hint>Plaintext in this browser, not encrypted or synced. Up to 20 drafts (64,000 characters each), expiring after seven days. Images are not saved. Disable on shared devices or when composing secrets. No messages are sent automatically.</Hint>
    <Button variant="secondary" onClick={() => { setNotice(deleteSavedDrafts() ? "Saved copies deleted. Text already open in an editor remains until cleared or closed." : "Could not delete saved drafts. Clear this site's storage in browser settings."); }}>Delete saved drafts</Button>
    {notice && <p role="status" className="text-sm text-muted">{notice}</p>}
  </Card>;
}

/** Plaintext, device-local text drafts only. Never stores attachments or queues sends.
 * Browser storage may be disabled/full; the in-memory composer must still work. */
const PREFIX = "openbot:draft:v1:";
const PREFERENCE = "openbot:save-drafts";
export const DRAFT_TTL = 7 * 24 * 60 * 60 * 1000;
const LIMIT = 20;
const MAX_TEXT = 64_000;
type Draft = { text: string; updated: number; uncertain: boolean };
export function savesDrafts(): boolean {
  try { return localStorage.getItem(PREFERENCE) !== "off"; } catch { return false; }
}
function records(): [string, Draft][] {
  const result: [string, Draft][] = [];
  for (const key of Object.keys(localStorage).filter((k) => k.startsWith(PREFIX))) {
    try {
      const value = JSON.parse(localStorage.getItem(key) ?? "null");
      if (!value || typeof value.text !== "string" || value.text.length > MAX_TEXT || !Number.isFinite(value.updated) || value.updated < Date.now() - DRAFT_TTL || value.updated > Date.now()) throw new Error("expired/invalid");
      result.push([key, value]);
    } catch { localStorage.removeItem(key); }
  }
  return result.sort((a, b) => b[1].updated - a[1].updated);
}
export function readDraft(id: string): Draft | undefined {
  try {
    if (!savesDrafts()) return;
    const all = records();
    for (const [key] of all.slice(LIMIT)) localStorage.removeItem(key);
    return all.slice(0, LIMIT).find(([key]) => key === PREFIX + id)?.[1];
  } catch { return; }
}
export function storeDraft(id: string, text: string, uncertain = false): void {
  try {
    if (!savesDrafts() || !text || text.length > MAX_TEXT) { localStorage.removeItem(PREFIX + id); return; }
    localStorage.setItem(PREFIX + id, JSON.stringify({ text, updated: Date.now(), uncertain }));
    for (const [key] of records().filter(([key]) => key !== PREFIX + id).slice(LIMIT - 1)) localStorage.removeItem(key);
  } catch {
    try { localStorage.removeItem(PREFIX + id); } catch { /* memory remains authoritative */ }
  }
}
export function deleteSavedDrafts(): boolean {
  try {
    for (const key of Object.keys(localStorage)) if (key.startsWith(PREFIX)) localStorage.removeItem(key);
    return true;
  } catch { return false; }
}
export function setSavesDrafts(enabled: boolean): void {
  // Surface preference failures to Settings rather than promising persistence.
  if (!enabled && !deleteSavedDrafts()) throw new Error("Could not delete saved drafts");
  localStorage.setItem(PREFERENCE, enabled ? "on" : "off");
}
